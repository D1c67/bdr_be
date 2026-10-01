# RFP Ingestion: file verdicts, re-check and IT release (held files still make a project)

Design contract for the seventh slice of RFP Ingestion. When the Ingestion
Sandbox (or the promotion gate behind it) refuses a harvested file, the
project is still created, the reason is recorded per file in one of five
tiers, the people who work the bid are told whether to worry, IT is alerted
for the suspicious ones and can release a file after inspecting it, and
files whose check never finished get one automatic retry and then a
"Re-check files" button. Files rescued or released after creation flow
through the Bid File Splitter like any other file when they are safe to
render.

Status: decisions taken with the user 2026-09-23 (section 1); revised
2026-09-24 after three adversarial reviews (section 14). DEV ONLY.
NUMBERING NOTE (2026-09-26): `0136` was taken by the BuildingConnected slice
(`0136_rfp_buildingconnected.sql`, docs/RFP_BUILDINGCONNECTED.md) before this
slice was built. Read every `0136` below as `0137`, and "Apply after 0136".

Migration 0136 applies to the dev database (BDR) only, never BDR_Prod
without explicit approval. No em dashes anywhere (docs, code comments, UI
text).

This file is the contract the builders share (section 9). Names,
signatures, column names, endpoint paths, i18n keys and sentences here are
final. Where a builder finds the code disagrees with a detail, the builder
follows the code's existing pattern and records the difference in its build
record (section 12, appended by the orchestrator from each builder's final
report; builders never edit this file).

Naming: pure module `app/services/rfp_file_verdicts.py`, orchestration
module `app/services/rfp_file_rescue.py`, setting prefixes `rfp_file_`
(env `RFP_FILE_`) plus `rfp_split_sandbox_wait_minutes` and
`rfp_processing_sandbox_wait_minutes`, migration
`0136_rfp_file_verdicts.sql`, notification types `rfp_create.files_*`,
FE module `lib/rfpFileVerdicts.ts`, FE namespace `rfpFiles.*` plus
additions under `projectPage`, `filesPanel`, `biddingLink`, `rfpCreated`,
`rfpEmails`, `rfpProcessing`.

Code map this contract was written from (read for background, not
authoritative): the session scratchpad `synthesis.txt` and `readers.json`
(2026-09-23). Every claim below about existing code carries its own
file:line, re-verified 2026-09-24.

---

## 1. Decisions

`USER` = locked by the user 2026-09-23 (do not re-ask). `BUILDER` = the
design author's choice, stated to the user or made here; a builder may not
change it without the orchestrator.

| # | Topic | Decision | Kind |
|---|---|---|---|
| D1 | Three held tiers plus two non-held states | Every file that does not reach the project gets exactly one tier. RED `unsafe`: "may be compromised, contact IT, do not open it from the RFP", only for truly suspicious files (JavaScript, launch actions, embedded files and file attachments, XFA, polyglot, `not_pdf` whose sniff shows executable, script or HTML content, bytes changed after verification, OOXML hazards, `hazards_unknown`). AMBER `held`: "held by file safety rules, IT notified", for common CAD / Bluebeam export features (`/AA`, remote GoTo / `/GoToR`, open and page actions). PLAIN `not_usable`: encrypted, too large, empty, duplicate, no pages, too many pages, unreadable, item attachment, not stored, conversion rejected, run page budget, too many failed pages, unsupported format and similar. Non-held `rerunnable`: sandbox FAIL codes, pending or running files under a terminal run, `verified_with_gaps`. Transient `checking`: the run is still active. Full table: section 3. | USER |
| D1a | `crash_loop` | RED `unsafe`, releasable whole only (never staged into the splitter). A file that crashed the PDFium child more than max(5, 5% of its pages) times (rfp_sandbox_runner.py:1518-1530) drove the parser into a fault repeatedly; that is the signature of a malformed or weaponized file, and the same parser family runs in desktop viewers and in the backend splitter (pdf_split.py:8-22). | BUILDER |
| D1b | `invalid_output` | FIRST occurrence `rerunnable` ("the sandbox could not confirm its own result"); a REPEAT on the same bytes after a re-check is RED `unsafe`, releasable whole only. Several triggers are parent-side or protocol faults, not properties of the file: a short page delivery (rfp_ingest.py:1880-1891), the fallback for any non-complete result without a code (rfp_ingest.py:2134), uncovered pages after `end` (rfp_sandbox_runner.py:1510-1511). A deploy skew would otherwise mark every file RED. The repeat is detected by `rfp_ingest_files.prior_fail_code` (new, written by every `retry_run` reset, section 5d). | BUILDER |
| D1c | `embedded_goto` | AMBER `held`. A GoToE action navigates into an embedded document; the embedded file itself, if present, is counted by `attachments` / `/EmbeddedFile`, which are RED. It is not harmless: like GoToR, a GoToE whose file specification is a UNC path can make a Windows viewer contact a remote host and leak the user's NTLM hash. That is why AMBER files are held for IT and never opened from the source; the inspect view shows counts, not targets (open question 2). | BUILDER |
| D1d | `/RichMedia` marker | RED `unsafe`. Embedded media and Flash content is a historical exploit carrier and has no place in a bid document. | BUILDER |
| D1e | Intake `download_failed` WITH a `sandbox_file_id` | CORRECTION to D1's "rerunnable" list, classified `not_usable` ("a new harvest is needed"). Verified: that entry status is written only when `add_upload_file` returned a `failed` row (rfp_harvest.py:1570-1572, rfp_portal_ingest.py:2524-2526), which happens only when the quarantine upload itself failed, leaving `quarantine_path` null (rfp_ingest.py:939-956). A sandbox rerun of such a row fails `storage` again forever (`_materialize`, rfp_ingest.py:1266-1269). D1's intent (rerun what can be rerun) is kept by the general rule "rerunnable needs stored bytes". Open question 1. | BUILDER |
| D1f | Worst key wins | A file's tier is the worst over ALL its findings: PDFium hazard keys, the byte markers of the stored copy (the name-token scan, recorded once per file in `rfp_ingest_files.promotion_scan`, section 5a) and the OOXML container verdict. So `page_actions` (AMBER) plus a `/JS` marker is RED. An unknown hazard or marker key (a future sandbox counter) is `unsafe` (fail closed). | BUILDER |
| D1g | Suspicious content by name | `not_pdf` is RED when the sniff's head magics show executable, script, HTML, RTF or PostScript content, OR the file's declared extension is an executable, script, shortcut, disk image, OneNote, Java / installer, macro-enabled Office, RTF, HTML or SVG type (`SUSPICIOUS_EXTENSIONS`, 3.1). The email harvester sends every non-image, non-email attachment to the sandbox (rfp_email_files.py:176-210), so malspam payloads arrive exactly this way. | BUILDER |
| D1h | Zip refusals | A zip the harvester refused as a decompression bomb is RED (a crafted artifact: more than 200:1 over 1 MB, rfp_zip.py:34-38, 185-188; no bytes kept, not releasable). A zip member skipped for an unsafe path name (path traversal) is RED. Encrypted, nested, empty and oversized members and a file that is not a zip are `not_usable`. All are listed on the project (they were not before). | BUILDER |
| D2 | IT Admin | Automatic bell plus mirror email to every active `it_admin` with the project and the new file list; deduped so re-runs never re-alert a file already alerted for the same finding. IT Admins get an audited in-app RELEASE of a held (`unsafe` or `held`) file after inspecting it, with an extra explicit confirm for RED. A released file runs through the Bid File Splitter when it is sandbox-verified and renderable, otherwise it is promoted whole. | USER |
| D2a | Who may release | `it_admin` role only (`require_role(Role.IT_ADMIN)`), not `is_dev` accounts. D2 names the IT Admin; `is_dev` is an orthogonal dev-tools axis (deps.py:280-290). | BUILDER |
| D2b | What IT inspects | The sandbox's own safe outputs only: the verdict facts (status, codes, hazard counters, the byte-marker scan of the stored copy, the OOXML container verdict, sniff magics, page count, sha256) and the derived page-images PDF, text and manifest (`rfp_ingest.file_urls`, rfp_ingest.py:2646-2683). The raw quarantine bytes are never served: `rfp_ingest_storage.signed_url` refuses the quarantine bucket by design (rfp_ingest_storage.py:500-510), and this slice keeps that invariant. | BUILDER |
| D2c | Release scope | A release is a verdict on one sandbox file (the bytes), stored on `rfp_ingest_files`. Any project whose harvest lists the same sandbox file (a `reused` entry, rfp_email_harvest.py:523-571) admits it on its next promotion pass, with the release note on every row it lands as (whole, segments and source set). The release asks for a pass in every project holding the file's run. `released_project_id` and the audit row name the project it was released from. | BUILDER |
| D2d | What a release lifts | A release lifts ONLY the findings IT reviewed, and binds to them. At release the server re-scans the stored copy (sha256, byte markers, OOXML container) and refuses when the full finding set differs from what IT was shown. It stores that set (`released_keys`) and the file status (`released_status`). Promotion then admits the bytes only while the live status equals `released_status` and every live finding is in `released_keys`; anything new re-holds the file (`released:changed`, RED, IT re-alerted). Per format: a PDF promotes the original; a `.docx` / `.xlsx` keeps the OOXML scan and its fallback to the converted PDF unless the reviewed set names that `ooxml:` reason; a `.doc` / `.xls` ALWAYS promotes the sandbox's converted PDF, never the legacy original (no macro scan exists for OLE2, rfp_create_files.py:13-15, 93-95). | BUILDER |
| D2e | Splitter admission for released files | Enforced at the staging choke point, `rfp_split._stage_row` (`may_stage`, section 3.4), so the split step's `_start`, `stage_late` and any future stager all refuse a released file that is not `splitter_ok` (for example a released `crash_loop` file reused by a later harvest). Hand uploads into an `rfp` split job are refused (section 5c), so an `rfp` job only ever holds sandbox-admitted bytes. | BUILDER |
| D3 | Failures | The project is created right away with what passed. ONE automatic retry of the rerunnable files `RFP_FILE_RECHECK_AUTO_MINUTES` (10) later; then the Re-check button. If the sandbox never finishes (run never terminal), the split step stops waiting after `RFP_SPLIT_SANDBOX_WAIT_MINUTES` (60), cancels the stuck run when no queue job owns it (so it becomes retryable and its files `rerunnable`), and the project is created. | USER |
| D3a | Deadline with an owning job | When a queue job still owns the run at the deadline (a slow but live run), the run is NOT canceled: the project is created, the unfinished files are `checking`, and the follow-up sweep files them (through the splitter) when the run ends. Upper bound: the run's queue ladder; its exhaustion fails the run, the files become `rerunnable`, and the automatic re-check follows. Refines D3's "every unfinished file is marked rerunnable"; open question 3. | BUILDER |
| D3b | Re-check before the automatic retry | Not offered: while the automatic retry is scheduled the button is disabled with "An automatic re-check runs at 3:42 PM" (D3 order: automatic retry, then the button). With `RFP_FILE_RECHECK_AUTO_MINUTES=0` the button is available at once. | BUILDER |
| D3c | After the one automatic retry | When the automatic retry ends (its own runs followed through, or refused for good) and files are still rerunnable, the Estimating Admin gets one bell plus mirror (`rfp_create.files_recheck_needed`). A transient refusal (a storage error, a job still on the run) never uses the retry up: it is rescheduled, at most 6 times. IT is not alerted for rerunnable files. | BUILDER |
| D3d | Where a re-check runs | Inside the promotion pass, never in a request or the queue sweep. "Re-check files" and the automatic retry both only MARK the record (`recheck_requested_at` / `recheck_auto = 'due'`) and ask for a pass; the pass (one per project, fenced on its claim token) reruns the sandbox on the rerunnable files, so every re-check state change is serialized with promotion. | BUILDER |
| D4 | After the estimator hand-off | Files rescued or released after `files.handoff_locked(project_id)` land as category `additional` ("Additional files") with an automatic per-file note, unsent; the Estimating Admin gets a bell and uses the existing "Send updates". Before the hand-off they land in their normal categories (split outputs or `category_for`). | USER |
| D4a | Splitter after hand-off | Never. After the hand-off a rescued or released file is promoted whole as `additional`, because every splitter output lands in a frozen initial category and `resync_after_run` already refuses locked projects (rfp_split.py:1292-1294). A whole-only release (not `splitter_ok`) must never be opened in the splitter by hand either. | BUILDER |
| D4b | Note by provenance | Under the lock the automatic note says what is true for that file: released by IT, held back at creation and now cleared, or (a file that was never held, from an invitation linked after the hand-off) simply added after the package was sent. | BUILDER |
| DF1 | One "Re-check files" action | Roles `estimating_admin`, `executive`, `it_admin` (`rfp_created.PAGE_ROLES`, rfp_created.py:74), on /rfp-created, on the project page and in the files modal. Re-check = rerun the sandbox on RERUNNABLE files only, then stage the late files into the harvest's existing split job (same `bid_split_jobs` row), and the splitter's resync files them; when the split flags are off, promote whole via `category_for`. "Retry documents" stays only for promotion job failures (`files_status` `failed` or `none`). | USER (default, not objected to) |
| DF2 | Warnings | Project header badge plus Callout; FilesPanel Callout; /rfp-created per-file list with tier and reason; a caution step on the bidding site Open button (`BiddingLinkButton`) and on the harvest card's source links when the project or harvest has RED or AMBER files, naming those files. The portal link is NOT hidden. | USER (default) |
| DF3 | Retention | Quarantine bytes are kept past the 14-day prune while a linked project has unresolved (`unsafe`, `held`, `rerunnable`, `checking`) files, hard cap `RFP_FILE_HOLD_RETENTION_DAYS` (60) from the run's creation. After expiry the Re-check and Release buttons are disabled with "a new harvest is needed". Every rerun and release checks run status and object existence (prune leaves `quarantine_path` set, rfp_ingest.py:2963-2983). | USER (default) |
| DF4 | Pre-creation harvest card | The email and portal detail harvest card shows each entry's live verdict inline (a Callout plus the per-file sentence), so a file rejected mid-run no longer reads "accepted". Where the verdict is only provisional (no download before creation), it says so. | USER (default) |
| DF5 | Test bench | `/rfp-testing` (`rfp_test.record`) gets events for the deadline, late staging, verdicts, alerts, re-checks, releases and hand-off landings. Records tagged with an ENDED session are frozen for this slice too: no follow-up, no automatic re-check, no bell. | USER (default) |
| DF6 | /rfp-processing | A new stuck kind `sandbox_wait` for a row at `split` whose harvest has waited on the sandbox longer than `RFP_PROCESSING_SANDBOX_WAIT_MINUTES` (20), email and portal rows alike. | USER (default) |
| DF7 | Scope | DEV ONLY. Subagents: Sonnet 5 for simple work, Opus 5.5 for hard work. | USER |
| B1 | Promotion walks every harvest of the project | Each promotion pass classifies every entry of every harvest linked to the project (the record's, every `rfp_harvests.project_id = X`, and the payload's). `files_skipped` is therefore the whole project's list, keyed by sandbox file id, with no per-harvest merge logic. | BUILDER |
| B2 | Notifications deep link | No FE change: a `rfp_create.files_*` row carries `project_id`, so the bell falls through to `/projects/{id}` (NotificationsBell.tsx:174-192) and the mirror email to the same page (`notification_email._deep_link`, notification_email.py:162+), where the Callout sits at the top. | BUILDER |
| B3 | Per-file reason text | The per-file sentence is the backend's app-authored English `message` in every locale (as the harvest card's `entry.error` is today, RfpHarvestBlock.tsx:984). Tier labels, callouts, buttons and modal chrome are translated in all six catalogs. | BUILDER |
| B4 | No new feature flag | Everything is dev only and ships behind the existing RFP flags; migration 0136 must be applied before the backend that reads its columns (section 11). `RFP_FILE_RECHECK_AUTO_MINUTES=0` disables the automatic retry. Re-check and release need the queue (`LLM_QUEUE_ENABLED`); they answer 503 without it. | BUILDER |
| B5 | Never re-add what a person removed | The record keeps `promoted_ids` (every sandbox file the project has held a row for). A pass never promotes one again once its rows are gone: a person deleted it. Background passes would otherwise resurrect deleted files. | BUILDER |

---

## 2. Current behavior (summary; details in the synthesis, section A)

- The create step never reads sandbox state (rfp_create.py:984-1001). Only
  the split step waits for verdicts, and only with both split flags and the
  queue on (rfp_split.py:502-514); the wait has no cap (rfp_split.py:420-445,
  config.py:735-736).
- One pure gate, `promotion_for` (rfp_create_files.py:286-320), plus the
  post-download checks in `_fetch_verified` / `_fetch_entry`
  (rfp_create_files.py:667-708) decide every file for both the split step
  (rfp_split.py:709-722) and the promotion job (rfp_create_files.py:866-911).
  A `hazard:` Skip returns before any download (rfp_create_files.py:300-304),
  so the byte-marker scan and the OOXML scan never run for a hazard-held file.
- Rejected, failed, pending and running files all become `Skip(status)`;
  `_FILE_SELECT` omits `reject_code` and `error` (rfp_create_files.py:133-136,
  298-299). `files_skipped` is rewritten per job as `[{file_path, reason}]`,
  capped at 200 (rfp_create_files.py:129, 937-944), and served only as a
  count (rfp_created.py:359).
- Hazards never reject a file (hazards.py:4-11); PDFium counts page actions
  by presence only and cannot see OpenAction or widget scripts (hazards.py:
  13-36). A `verified` file with hazards or byte markers is refused only by
  promotion (`hazard:<keys>`, `marker:<keys>`). Users will see those as "the
  sandbox rejected it".
- A rejected file never makes a run `done_with_errors`
  (rfp_ingest.py:2304-2320). Pipeline runs have `created_by` null, so the
  finished bell never fires (rfp_ingest.py:634-639, 785-803). IT Admin is
  never notified.
- The only sandbox rerun is dev-only and run-scoped (`POST
  /rfp-ingest/runs/{id}/retry`, require_dev, ai_rate_limit; `retry_run`,
  rfp_ingest.py:2421-2501); it never touches rejected or gapped files and
  refuses `done` runs. "Retry documents" re-runs promotion only
  (rfp_created.py:540-593) and re-skips the same files.
- Nothing re-splits after creation: `advance` returns early on a terminal
  `split_status` or a linked harvest (rfp_split.py:499-508). A late promotion
  has no hand-off lock check (rfp_create_files.py:813-951), unlike
  `resync_after_run` (rfp_split.py:1292-1294).
- A whole-run failure leaves files `pending` (self-test, spawn abort,
  rfp_ingest.py:2329-2339, 2374-2377); the project is created with zero files
  and nobody is told. A run that never turns terminal keeps the row at
  `split` forever and /rfp-processing never calls it stuck (each poll bumps
  `updated_at`; rfp_processing.py:196-207).
- An intake reject rewrites the harvest entry (`rejected` /
  `download_failed`, rfp_harvest.py:1566-1575); a mid-run reject leaves the
  entry `accepted`, which the pre-creation card shows (RfpHarvestBlock.tsx:
  917-994). When every file is rejected at intake the run is deleted
  (rfp_harvest.py:1589-1596) and `_enqueue_files_job` enqueues nothing
  (`_entries_with_files`, rfp_create.py:1084-1088, 1168-1169), so the
  project shows "missing files" and no reason at all. A refused zip (bomb,
  not a zip) is an entry `rejected` with only an error sentence
  (rfp_email_harvest.py:404-413); skipped zip members live only in
  `data.attachments.skipped` (rfp_email_harvest.py:363-380, 617-621).
- Retention: `prune_expired` expires terminal runs 14 days after
  `completed_at` and deletes both prefixes but leaves `quarantine_path` set
  (rfp_ingest.py:2963-2983, 3039-3083).
- `POST /bid-splitter/jobs/{job_id}/files` (any writer) accepts a hand
  upload into an `rfp` job (bid_splitter.py:250-290, `_job_or_404` does not
  check `source`, :66-72), and `resync_after_run` would file it with RFP
  provenance (rfp_split.py:1241-1244). This slice closes that (section 5c).

---

## 3. Taxonomy

### 3.1 The pure module

`app/services/rfp_file_verdicts.py` (package A). Imports only the standard
library and `app.sandbox.protocol` (a test enforces it). Everything in it is
pure and deterministic.

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, TypedDict

Tier = Literal["unsafe", "held", "not_usable", "rerunnable", "checking"]

TIER_UNSAFE, TIER_HELD, TIER_NOT_USABLE, TIER_RERUNNABLE, TIER_CHECKING = (
    "unsafe", "held", "not_usable", "rerunnable", "checking")
# Severity order: sorting, "worst tier wins", the tier counts' key order.
TIERS: tuple[Tier, ...] = ("unsafe", "held", "rerunnable", "checking", "not_usable")
HELD_TIERS = frozenset({"unsafe", "held"})                     # IT alert, release
OPEN_TIERS = frozenset({"unsafe", "held", "rerunnable", "checking"})  # retention, callouts

@dataclass(frozen=True)
class Verdict:
    tier: Tier
    code: str                      # stable machine code, section 3.3
    message: str                   # English, app-authored, never exception or child text
    keys: tuple[str, ...] = ()     # every finding behind the verdict (hazard keys, marker
                                   # names, "ooxml:<why>", "<status>:<code>"), sorted
    may_release: bool = False      # tier in HELD_TIERS and the code is releasable (3.4)
    may_recheck: bool = False      # tier == rerunnable, the run is terminal and not expired
    listed: bool = True            # False only for an opened zip container

def classify(
    reason: str,
    *,
    entry: Mapping[str, Any],
    file_row: Mapping[str, Any] | None,
    run_status: str | None,
) -> Verdict: ...
```

`reason` is the `Skip.reason` the promotion gate produced for this entry
(`promotion_for`, or the fetch after it, or one of the pass's own reasons
`split:running`, `promotion:removed`, `released:changed`, section 5a).
`file_row` must carry the columns of the new `_FILE_SELECT` (5a):
`status, reject_code, hazards, manifest, source_format, converted_path,
quarantine_path, sha256, promotion_scan, prior_fail_code, released_at,
released_status, released_keys`. `classify` decides in this precedence
order, first match wins:

1. Entry level: no `sandbox_file_id`, or `entry.status` not in
   `ENTRY_WITH_FILE = ("accepted", "reused")` (a copy of
   rfp_create_files.py:84; a test asserts equality): rows E1 to E16 by
   `entry.status`, `entry.reject_code` (new, section 4.4; else the code
   whose `protocol.VERDICT_MESSAGES` sentence equals `entry.error`),
   `entry.sniff_magics` (new) and the entry name's extension; rows Z1 to
   Z5 for a synthetic skipped-member entry (`status == "skipped_member"`,
   5a step 4).
2. `file_row is None`: row P8 (`promotion:not_in_sandbox`).
3. `file_row.released_at` is set: `reason == "released:changed"` -> row
   P12; any other reason -> the P / M / O row for that reason (a released
   file whose download then failed shows the real cause, never its old
   status row).
4. `file_row.status` in (`pending`, `running`): rows F1 to F3 by
   `run_status`.
5. `run_status` in `protocol.RUN_ACTIVE_STATUSES` and `file_row.status` in
   (`failed`, `verified_with_gaps`): row F4 (`checking`). A file cannot be
   re-checked while its run is busy, and the run's end triggers the
   follow-up.
6. `file_row.status == "rejected"`: rows R1 to R17 by `reject_code`
   (`not_pdf` split by magic and extension, D1g).
7. `file_row.status == "failed"`: rows X1a to X8 by `reject_code`
   (`invalid_output` split by `prior_fail_code`, D1b).
8. `file_row.status == "verified_with_gaps"`: row G1.
9. `file_row.status == "verified"`: rows V1, H*, M*, O*, P*, S1 by
   `reason`. For `hazard:*` and `hazards_unknown` the verdict's keys are
   `live_keys(file_row)` (hazard keys plus the valid scan's markers and
   OOXML reason) and the tier is the worst over all of them (D1f).
10. Any status the module does not know: row P10 (`promotion:unknown`).

Then, for any `rerunnable` verdict with `run_status == "expired"`:
`may_recheck = False` and the message gains " The stored copy has expired;
a new harvest is needed." For any `unsafe` / `held` verdict with
`run_status == "expired"`: `may_release = False` (same suffix).

Other pure names in the module (signatures final):

```python
ENTRY_WITH_FILE: tuple[str, ...] = ("accepted", "reused")
ENTRY_SKIPPED_MEMBER = "skipped_member"
ALLOWED_HAZARDS = frozenset({"uri_links"})          # == rfp_create_files.ALLOWED_HAZARDS (test)
ALLOWED_MARKERS = frozenset({"/URI"})               # == rfp_create_files.ALLOWED_MARKERS (test)
SUSPICIOUS_HEAD_MAGICS = frozenset({"mz", "elf", "script", "html", "doctype", "rtf", "ps"})
SUSPICIOUS_EXTENSIONS = frozenset({
    ".exe", ".dll", ".scr", ".com", ".pif", ".cpl", ".msi", ".msp", ".msc", ".bat", ".cmd",
    ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".jar", ".lnk",
    ".url", ".scf", ".reg", ".chm", ".iso", ".img", ".vhd", ".vhdx", ".one", ".onepkg",
    ".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".xll", ".pptm", ".potm", ".ppam",
    ".rtf", ".html", ".htm", ".xhtml", ".svg", ".sh", ".apk", ".appx", ".msix",
    ".application", ".appref-ms"})
RED_MARKERS = frozenset({"/JavaScript", "/JS", "/Launch", "/EmbeddedFile", "/XFA", "/RichMedia"})
REJECT_TIERS: dict[str, Tier]       # every protocol.REJECT_CODES code
FAIL_TIERS: dict[str, Tier]         # every protocol.FAIL_CODES code (invalid_output: its first occurrence)
HAZARD_TIERS: dict[str, Tier]       # DOC_HAZARD_KEYS + PAGE_HAZARD_KEYS minus ALLOWED_HAZARDS
MARKER_TIERS: dict[str, Tier]       # decoded BYTE_MARKERS minus ALLOWED_MARKERS
PROMOTION_TIERS: dict[str, Tier]    # every rfp_create_files.REASON_* value
OOXML_TIER: Tier = "unsafe"         # every ooxml:<why>
ENTRY_TIERS: dict[str | None, Tier] # every rfp_harvest FILE_* status plus None
ENTRY_REJECT_TIERS: dict[str, Tier] # entry-only reject codes: "zip_bomb", "not_zip"
ZIP_MEMBER_TIERS: dict[str, Tier]   # rfp_zip skipped reasons minus "image"
NEVER_RELEASE = frozenset({"rejected:polyglot", "rejected:not_pdf_active",
                           "rejected:zip_bomb", "zip:unsafe_name",
                           "promotion:changed_since_verification",
                           "promotion:converted_sha_mismatch"})

class SkippedEntry(TypedDict):      # section 4.1
    ...

def worst(tiers) -> Tier: ...
def key_tier(key: str) -> Tier: ...                  # one finding: hazard key, marker, "ooxml:*", "<status>:<code>"
def entry_key(entry: Mapping[str, Any], harvest_id: str | None) -> str: ...
def clean_name(value: Any, limit: int = 200) -> str: ...
def has_suspicious_extension(name: str) -> bool: ...
def scan_of(file_row: Mapping[str, Any] | None) -> Mapping[str, Any] | None: ...
def live_keys(file_row: Mapping[str, Any]) -> frozenset[str]: ...
def provisional_red_markers(file_row: Mapping[str, Any]) -> list[str]: ...
def alert_signature(entry: Mapping[str, Any]) -> str: ...
def to_skipped(verdict: Verdict, *, reason: str, entry: Mapping[str, Any],
               file_row: Mapping[str, Any] | None, harvest_id: str | None,
               name: str) -> SkippedEntry: ...
def counts(entries) -> dict[str, int]: ...          # listed entries only, keys = TIERS
def sort_and_cap(entries: list[SkippedEntry], cap: int) -> list[SkippedEntry]: ...
def legacy_tier(reason: str) -> Tier: ...           # {file_path, reason} rows written before 0136
def splitter_ok(file_row: Mapping[str, Any]) -> bool: ...
def release_note(when: datetime, tz: str) -> str: ...
def handoff_note(kind: Literal["released", "rescued", "added"], *, when: datetime, tz: str) -> str: ...
def held_alert_message(label: str, entries: list[Mapping[str, Any]]) -> str: ...
def recheck_needed_message(label: str, count: int) -> str: ...
def added_after_handoff_message(label: str, counts: Mapping[str, int]) -> str: ...
```

- `clean_name`: `unicodedata.normalize("NFC", str(value or ""))`, then every
  character whose Unicode category is `Cc`, `Cf`, `Co`, `Cs`, `Zl` or `Zp`
  removed (control characters, bidi overrides and isolates U+202A to
  U+202E and U+2066 to U+2069, U+061C, zero-width U+200B to U+200F,
  U+FEFF, line and paragraph separators, private use), whitespace
  collapsed, stripped, capped at `limit` (a trailing "..." inside the limit
  when cut); empty becomes "document". Every name that reaches a message,
  an alert, `files_skipped`, a bell or the release modal goes through it.
- `entry_key(entry, harvest_id)`: `entry.sandbox_file_id` when set;
  `"{harvest_id}:skipped:{file_path}"` for a skipped-member entry;
  else `"{harvest_id}:{file_path}"`.
- `has_suspicious_extension(name)`: the lowercased suffix after the last
  "." of `clean_name(name)` is in `SUSPICIOUS_EXTENSIONS`.
- `scan_of(file_row)`: `file_row.promotion_scan` when it is a dict whose
  `sha256` equals `file_row.sha256` (both lowercased, non-empty), else None
  (a scan of other bytes is no scan).
- `live_keys(file_row)`: for `verified`: the hazard keys above zero minus
  `ALLOWED_HAZARDS` (or `{"hazards_unknown"}` when the block is not a dict),
  plus the valid scan's `marker_keys`, plus `"ooxml:" + scan.ooxml` when
  set; for `rejected` / `failed`: `{f"{status}:{reject_code}"}` plus the
  valid scan's markers; else empty.
- `provisional_red_markers(file_row)`: the `RED_MARKERS` whose
  `manifest.sniff.byte_markers` substring count is above zero, sorted. Used
  only for the pre-creation card (no download); `/AA` is never used from
  the substring counts (it hits every `/AAPL` of a Mac-made PDF).
- `alert_signature(entry)`: `f"{entry['tier']}:{entry['code']}"`.
- `splitter_ok(file_row)`: `status == "verified"` AND (`source_format ==
  "pdf"`, or `source_format` in the office formats AND `converted_path` is
  set) AND no key of `released_keys` starts with `ooxml:`. Anything else is
  promoted whole.
- `legacy_tier(reason)`: `hazard:*` / `marker:*` by the worst key; `ooxml:*`,
  `changed_since_verification`, `converted_sha_mismatch`, `hazards_unknown`
  `unsafe`; `failed`, `pending`, `running`, `pages_unverified` `rerunnable`;
  everything else `not_usable`.
- `release_note(when, tz)`: "Held back by the file safety checks; released by
  IT on {date}." `handoff_note("released")`: "Held back when the project was
  created; released by IT on {date}." `handoff_note("rescued")`: "Held back
  when the project was created; cleared by the file safety check on {date}."
  `handoff_note("added")`: "Added from the RFP invitation on {date}, after
  the estimator package was sent." `{date}` is `when` in `tz`
  (`settings.display_timezone`, config.py:422) formatted "Sep 24, 2026".
  All fit `FILE_NOTE_MAX_CHARS` (2000, file_categories.py:170).
- The message builders are specified in section 7.

### 3.2 Exhaustiveness rule (enforced by `tests/test_rfp_file_verdicts.py`)

The test fails when any of these is not true:

1. `set(REJECT_TIERS) == protocol.REJECT_CODES` and `set(FAIL_TIERS) ==
   protocol.FAIL_CODES` (protocol.py:360-413).
2. `set(HAZARD_TIERS) == (set(DOC_HAZARD_KEYS) | set(PAGE_HAZARD_KEYS)) -
   rfp_create_files.ALLOWED_HAZARDS` (protocol.py:256-264,
   rfp_create_files.py:87), and `ALLOWED_HAZARDS` / `ALLOWED_MARKERS` equal
   the `rfp_create_files` constants.
3. `set(MARKER_TIERS) == {m.decode() for m in BYTE_MARKERS} -
   rfp_create_files.ALLOWED_MARKERS` (protocol.py:265-276,
   rfp_create_files.py:90), and every `RED_MARKERS` member is `unsafe` in
   `MARKER_TIERS`.
4. Every module-level `REASON_*` string in `rfp_create_files` is a key of
   `PROMOTION_TIERS` (collected with `vars(rcf)`), and every member of
   `rfp_create_files.OOXML_REASONS` (new constant, section 5a) classifies as
   `unsafe`.
5. Every `rfp_harvest.FILE_*` status (rfp_harvest.py:97-106) plus `None` is a
   key of `ENTRY_TIERS`; `set(ZIP_MEMBER_TIERS)` equals the `reason = "..."`
   string literals assigned in `rfp_zip.inspect` minus `"image"` (collected
   with an `ast` walk of rfp_zip.py); `set(ENTRY_REJECT_TIERS)` covers every
   `ZipListing.error_kind` except `"too_large"` (rfp_zip.py:76) under its
   `zip_bomb` / `not_zip` name.
6. For every `file_status` in `protocol.FILE_STATUSES` and every
   `run_status` in `protocol.RUN_STATUSES | {None}`, `classify` returns a
   Verdict whose tier is in `TIERS` (with `reason` set to what
   `promotion_for` returns for that row), and `may_recheck` is True only
   when `run_status` is terminal and not `expired`.
7. Every Verdict the table can produce has a non-empty `message` of at most
   300 characters containing no U+2014 (em dash), no U+2013, no "{", no
   "Traceback", no "Exception".
8. `ENTRY_WITH_FILE == rfp_create_files.ENTRY_WITH_FILE`.
9. The module source imports nothing but the standard library and
   `app.sandbox.protocol` (ast walk).
10. The table in section 3.3 of this document and the module agree for the
    codes the test enumerates (the test holds its own copy of the expected
    `code -> tier` map, typed from section 3.3), including these escalation
    cases: `page_actions` with a scan marker `/JS` -> `unsafe`;
    `remote_goto` with `/Launch` -> `unsafe`; `embedded_goto` alone ->
    `held`; `not_pdf` named `invoice.js` with no magic -> `unsafe`;
    `invalid_output` with and without `prior_fail_code == "invalid_output"`.

### 3.3 The table

Columns: row id, where the verdict comes from, code, tier, the user-facing
sentence (the Verdict `message`), whether the IT Admin may release it
(static rule; runtime conditions in 3.4), and why. `ENTRY` = harvest entry
level; `FILE` = `rfp_ingest_files` row; `PROMO` = the promotion gate on a
`verified` row.

#### Harvest entry level

| Id | Source | Code | Tier | Sentence | Release | Why |
|---|---|---|---|---|---|---|
| E1 | ENTRY `rejected`, code `polyglot` | `rejected:polyglot` | unsafe | The file is disguised: it starts as another kind of file and hides a PDF inside. | No | A disguised file is the classic delivery trick; no copy is kept at intake (rfp_ingest.py:913-920, 936-937). |
| E2 | ENTRY `rejected`, code `not_pdf`, a sniff magic in SUSPICIOUS_HEAD_MAGICS or a name in SUSPICIOUS_EXTENSIONS | `rejected:not_pdf_active` | unsafe | The file is a program, script, shortcut or web page, not a document. | No | D1g: executable, script, HTML, RTF or PostScript content, or a malspam file type posing as a bid file (rfp_sanitize.py:89-104, 258-275); no copy kept. |
| E3 | ENTRY `rejected`, code `not_pdf`, otherwise | `rejected:not_pdf` | not_usable | The file is not a PDF, Word or Excel file. | No | An unsupported format, not a threat signal. |
| E4 | ENTRY `rejected`, code `empty` | `rejected:empty` | not_usable | The file is empty. | No | Nothing to use. |
| E5 | ENTRY `rejected`, code `too_large` | `rejected:too_large` | not_usable | The file is larger than the per-file limit. | No | A size cap. |
| E6 | ENTRY `rejected`, any other known sandbox reject code | same as rows R1 to R17 | per R row | per R row | per R row | The same verdict whichever side recorded it. |
| E7 | ENTRY `rejected`, code unknown (legacy entry, error not a known sentence) | `entry:rejected` | not_usable | The file was refused when it was received. | No | Nothing more is known. |
| E8 | ENTRY `too_large` | `entry:too_large` | not_usable | The file is larger than the sandbox accepts. | No | A size cap (a zip declaring too many bytes lands here too, rfp_email_harvest.py:407-409). |
| E9 | ENTRY `download_failed` with a `sandbox_file_id` | `entry:not_stored` | not_usable | The file could not be stored for checking; a new harvest is needed. | No | D1e: no bytes were kept, a rerun cannot help. |
| E10 | ENTRY `download_failed` without a `sandbox_file_id` | `entry:download_failed` | not_usable | The file could not be downloaded from the invitation; a new harvest is needed. | No | A harvest failure, not a sandbox verdict. |
| E11 | ENTRY `skipped_cap` | `entry:skipped_cap` | not_usable | The invitation had more files than one check accepts; this file was not checked. | No | The per-run file cap (rfp_harvest.py:1554-1559). |
| E12 | ENTRY `expanded` | `entry:expanded` | not_usable, `listed = False` | A zip archive; the files inside it are listed on their own. | No | A container, not a document; hidden from lists and counts. |
| E13 | ENTRY status null | `entry:not_downloaded` | not_usable | The file was never downloaded. | No | The harvest stopped before it. |
| E14 | ENTRY any other status, or `accepted` / `reused` with no `sandbox_file_id` | `entry:other` | not_usable | The file was not downloaded for checking. | No | Fail safe for an unknown status. |
| E15 | ENTRY `rejected`, `reject_code == "zip_bomb"` | `rejected:zip_bomb` | unsafe | The zip archive is built to overwhelm the computer that opens it. | No | D1h; nothing kept (the scratch copy is deleted, rfp_email_harvest.py:410-412). |
| E16 | ENTRY `rejected`, `reject_code == "not_zip"` | `rejected:not_zip` | not_usable | The file is named like a zip archive but cannot be opened as one. | No | D1h. |

#### Skipped zip members (synthetic entries, 5a step 4)

| Id | Member reason (rfp_zip.py:171-186) | Code | Tier | Sentence | Release | Why |
|---|---|---|---|---|---|---|
| Z1 | `unsafe_name` | `zip:unsafe_name` | unsafe | A file inside the zip has a path built to escape its folder. | No | D1h: path traversal is a hostile signal; never extracted. |
| Z2 | `encrypted` | `zip:encrypted` | not_usable | A file inside the zip is password-protected and could not be checked. | No | D1h; the same tier as an encrypted PDF (R1). |
| Z3 | `nested_zip` | `zip:nested_zip` | not_usable | A zip inside the zip is not opened. | No | D1h. |
| Z4 | `too_large` | `zip:too_large` | not_usable | A file inside the zip is larger than the per-file limit. | No | D1h. |
| Z5 | `empty` | `zip:empty` | not_usable | A file inside the zip is empty. | No | D1h. |

#### File rows, pending or running, and busy runs

| Id | Source | Code | Tier | Sentence | Release | Why |
|---|---|---|---|---|---|---|
| F1 | FILE `pending` / `running`, run `staging`, `pending`, `running` or unknown | `checking:sandbox` | checking | The sandbox is still checking this file. | No | The run is alive; the follow-up sweep files it when the run ends (5c). |
| F2 | FILE `pending` / `running`, run `done`, `done_with_errors`, `failed` or `canceled` | `rerunnable:stopped` | rerunnable | The check stopped before this file was finished. | No | Self-test failure, spawn abort, cancel, ladder exhaustion (rfp_ingest.py:596-617, 2329-2339, 2374-2377); rerunnable by `retry_run`. |
| F3 | FILE `pending` / `running`, run `expired` | `rerunnable:stopped` | rerunnable, `may_recheck = False` | The check stopped before this file was finished. The stored copy has expired; a new harvest is needed. | No | Bytes deleted by retention. |
| F4 | FILE `failed` / `verified_with_gaps`, run `staging`, `pending` or `running` | `checking:run_busy` | checking | The sandbox is busy with this file's check run; it is looked at again when the run ends. | No | `retry_run` refuses an active run (rfp_ingest.py:2433-2443); the run's end triggers the follow-up, which reclassifies it. |

#### File rows, rejected (every `protocol.REJECT_CODES` code)

| Id | Code (`reject_code`) | Verdict code | Tier | Sentence | Release | Why |
|---|---|---|---|---|---|---|
| R1 | `encrypted` | `rejected:encrypted` | not_usable | The PDF is password-protected and cannot be processed. | No | D1 plain. |
| R2 | `unreadable` | `rejected:unreadable` | not_usable | The PDF could not be opened by the sandbox. | No | D1 plain. |
| R3 | `no_pages` | `rejected:no_pages` | not_usable | The PDF has no pages. | No | D1 plain. |
| R4 | `too_many_pages` | `rejected:too_many_pages` | not_usable | The PDF has more pages than the per-file limit. | No | D1 plain. |
| R5 | `not_pdf`, a head magic in SUSPICIOUS_HEAD_MAGICS (`manifest.sniff.markers_in_head`) or a name in SUSPICIOUS_EXTENSIONS | `rejected:not_pdf_active` | unsafe | The file is a program, script, shortcut or web page, not a document. | No | As E2. |
| R6 | `not_pdf`, otherwise | `rejected:not_pdf` | not_usable | The file is not a PDF, Word or Excel file. | No | As E3. |
| R7 | `polyglot` | `rejected:polyglot` | unsafe | The file is disguised: it starts as another kind of file and hides a PDF inside. | No | As E1; never a document worth releasing. |
| R8 | `too_large` | `rejected:too_large` | not_usable | The file is larger than the per-file limit. | No | D1 plain. |
| R9 | `empty` | `rejected:empty` | not_usable | The file is empty. | No | D1 plain. |
| R10 | `duplicate` | `rejected:duplicate` | not_usable | A file with identical content is already in this check; that copy is used instead. | No | The twin is promoted. |
| R11 | `item_attachment` | `rejected:item_attachment` | not_usable | The attachment is an embedded item, not a file. | No | D1 plain. |
| R12 | `not_stored` | `rejected:not_stored` | not_usable | The attachment content is no longer available. | No | D1 plain. |
| R13 | `run_page_budget` | `rejected:run_page_budget` | not_usable | The file would have gone past the page budget of one check. | No | D1 plain. |
| R14 | `crash_loop` | `rejected:crash_loop` | unsafe | The file crashed the sandbox's PDF reader again and again. | Yes, whole only | D1a. |
| R15 | `too_many_failed_pages` | `rejected:too_many_failed_pages` | not_usable | Too many pages failed to render. | No | D1 plain. |
| R16 | `conversion_rejected` | `rejected:conversion_rejected` | not_usable | The file could not be converted to PDF. | No | D1 plain. |
| R17 | any other or null | `rejected:unknown` | not_usable | The sandbox refused the file. | No | Fail safe. |

#### File rows, failed (every `protocol.FAIL_CODES` code), run terminal

| Id | Code | Verdict code | Tier | Sentence | Release | Why |
|---|---|---|---|---|---|---|
| X1a | `invalid_output`, `prior_fail_code` not `invalid_output` | `failed:invalid_output` | rerunnable | The sandbox could not confirm its own result for this file. | No | D1b: first occurrence; often a parent-side or protocol fault. |
| X1b | `invalid_output`, `prior_fail_code == "invalid_output"` | `failed:invalid_output_repeat` | unsafe | The file failed the sandbox's integrity check twice. | Yes, whole only | D1b: the repeat on the same bytes is the tamper signal. |
| X2 | `storage` | `failed:storage` | rerunnable | A storage step failed while checking the file. | No | Infrastructure. |
| X3 | `spawn` | `failed:spawn` | rerunnable | The sandbox could not start for this file. | No | Infrastructure. |
| X4 | `resource_limit` | `failed:resource_limit` | rerunnable | The check ran out of time or space. | No | Usually an undersized box; a real bomb fails the same way again, harmlessly, in the sandbox. |
| X5 | `interrupted` | `failed:interrupted` | rerunnable | The check was interrupted. | No | Infrastructure. |
| X6 | `orphaned` | `failed:orphaned` | rerunnable | The file was never checked. | No | Infrastructure. |
| X7 | `conversion_unavailable` | `failed:conversion_unavailable` | rerunnable | The Word or Excel converter was unavailable. | No | Gotenberg down (rfp_ingest.py:1464-1469). |
| X8 | any other or null | `failed:unknown` | rerunnable | The check failed. | No | `failed` is retryable by definition (protocol.py:392-393). |

(X1a and X2 to X8 with run `expired`: `may_recheck = False`, expiry suffix.
Under an active run every row here is F4.)

#### File rows, gaps

| Id | Source | Code | Tier | Sentence | Release | Why |
|---|---|---|---|---|---|---|
| G1 | FILE `verified_with_gaps` (promotion `pages_unverified`), run terminal | `gaps:pages_unverified` | rerunnable | Some pages could not be checked. | No | D1; a page gap is often memory pressure. The Re-check resets gapped files (5d). |

#### Verified rows refused by the promotion gate (PROMO)

For every `hazard:` row the verdict's `keys` are `live_keys(file_row)` and
the tier is the worst over them (D1f): a H6 to H8 file whose scan found
`/JS` is `unsafe` with the sentence of `/JS`.

| Id | `Skip.reason` | Code | Tier | Sentence | Release | Why |
|---|---|---|---|---|---|---|
| V1 | `hazards_unknown` | `hazard:unknown` | unsafe | The sandbox could not tell whether the file carries active content. | Yes | D1; unknown is never clean (rfp_create_files.py:219-224). Splitter allowed when verified. |
| H1 | `hazard:` key `javascript_actions` | `hazard:<keys>` | unsafe | The file contains JavaScript. | Yes | D1. |
| H2 | key `launch_actions` | `hazard:<keys>` | unsafe | The file contains actions that launch other programs. | Yes | D1. |
| H3 | key `attachments` | `hazard:<keys>` | unsafe | The file carries embedded files. | Yes | D1. |
| H4 | key `file_attachments` | `hazard:<keys>` | unsafe | The file carries file attachments on its pages. | Yes | D1. |
| H5 | key `xfa_packets` | `hazard:<keys>` | unsafe | The file contains an XFA form, which can run scripts. | Yes | D1. |
| H6 | key `remote_goto` | `hazard:<keys>` | held | The file has links that open other files. | Yes | D1 amber: common in CAD sheet sets; escalates with a RED scan marker. |
| H7 | key `embedded_goto` | `hazard:<keys>` | held | The file has links into embedded documents. | Yes | D1c. |
| H8 | key `page_actions` | `hazard:<keys>` | held | The file has actions that run when pages open or close. | Yes | D1 amber; PDFium counts presence only (hazards.py:28-31), so the scan decides whether a script is behind it. |
| H9 | any other key | `hazard:<keys>` | unsafe | The file carries active content of an unknown kind. | Yes | D1f fail closed. |
| M1 | `marker:` key `/JavaScript` | `marker:<keys>` | unsafe | The file contains JavaScript. | Yes | D1. |
| M2 | key `/JS` | `marker:<keys>` | unsafe | The file contains JavaScript. | Yes | D1. |
| M3 | key `/Launch` | `marker:<keys>` | unsafe | The file contains actions that launch other programs. | Yes | D1. |
| M4 | key `/EmbeddedFile` | `marker:<keys>` | unsafe | The file carries embedded files. | Yes | D1. |
| M5 | key `/XFA` | `marker:<keys>` | unsafe | The file contains an XFA form, which can run scripts. | Yes | D1. |
| M6 | key `/RichMedia` | `marker:<keys>` | unsafe | The file contains embedded media or Flash content. | Yes | D1d. |
| M7 | key `/OpenAction` | `marker:<keys>` | held | The file runs an action when it opens (often only "open at page N"). | Yes | D1 amber. |
| M8 | key `/AA` | `marker:<keys>` | held | The file has automatic actions (common in CAD and Bluebeam exports). | Yes | D1 amber; PDFium does not count widget actions, the marker scan does (hazards.py:34-36). |
| M9 | key `/GoToR` | `marker:<keys>` | held | The file has links that open other files. | Yes | D1 amber. |
| M10 | any other key | `marker:<keys>` | unsafe | The file carries active content of an unknown kind. | Yes | D1f. |
| O1 | `ooxml:vba_project`, `ooxml:macro_enabled` | `ooxml:<why>` | unsafe | The Word or Excel file contains macros. | Yes, whole only | D1 OOXML hazard; no converted PDF to fall back on (rfp_create_files.py:269-273). |
| O2 | `ooxml:embedding`, `ooxml:binary_part` | `ooxml:<why>` | unsafe | The Word or Excel file contains embedded objects or binary parts. | Yes, whole only | D1. |
| O3 | `ooxml:external_link`, `ooxml:external_rel:<word>` | `ooxml:<why>` | unsafe | The Word or Excel file loads content from outside the file. | Yes, whole only | D1. |
| O4 | `ooxml:dde` | `ooxml:<why>` | unsafe | The Word or Excel file uses DDE links. | Yes, whole only | D1. |
| O5 | `ooxml:too_many_members`, `ooxml:member_too_large`, `ooxml:bad_zip`, any other | `ooxml:<why>` | unsafe | The Word or Excel file is built in a way the safety scan cannot vouch for. | Yes, whole only | D1; fail closed. |
| P1 | `changed_since_verification` | `promotion:changed_since_verification` | unsafe | Our stored copy of this file changed after it was checked; IT has been told. | No | D1 (user-locked RED); release needs a matching sha256, which fails by definition. |
| P2 | `converted_sha_mismatch` | `promotion:converted_sha_mismatch` | unsafe | Our stored PDF copy of this file changed after it was checked; IT has been told. | No | D1; as P1. |
| P3 | `missing_in_storage` | `promotion:missing_in_storage` | not_usable | The stored copy is gone; a new harvest is needed. | No | Retention or a delete. |
| P4 | `no_digest` | `promotion:no_digest` | not_usable | The sandbox kept no fingerprint for this file. | No | An internal inconsistency, not a file property. |
| P5 | `no_source_object` | `promotion:no_source_object` | not_usable | No stored copy of this file was kept. | No | `quarantine_path` null (abandoned staging, rfp_ingest.py:3026-3028). |
| P6 | `unsupported_format` | `promotion:unsupported_format` | not_usable | The file type is not supported. | No | D1 plain. |
| P7 | `too_large` (the project file cap on download) | `promotion:too_large` | not_usable | The file is larger than a project file can be. | No | `upload_max_bytes`. |
| P8 | `not_in_sandbox` (no file row) | `promotion:not_in_sandbox` | not_usable | The sandbox has no record of this file; a new harvest is needed. | No | Row deleted. |
| P9 | `source_set:<reason>` (from `promote_split_file`, rfp_split.py:1166) | `promotion:source_set` | not_usable | The documents cut from this file were added; the uncut copy could not be kept. | No | The segments are in; only the reference copy is missing. |
| P11 | `promotion:removed` (new, 5a) | `promotion:removed` | not_usable | Removed from the project by a person; it is not added again. | No | B5. |
| P12 | `released:changed` (new, 5a) | `released:changed` | unsafe | The file changed after IT released it; it is held again and IT has been told. | Yes (a new release after review) | D2d: the live status or findings left the released set. |
| S1 | `split:running` (new, 5a) | `split:running` | checking | The Bid File Splitter is still working on this file. | No | A late split in flight. |
| P10 | any other reason | `promotion:unknown` | not_usable | The file could not be added to the project. | No | Fail safe for a code path this table missed. |

Row count: 16 entry rows, 5 zip-member rows, 4 pending / running / busy
rows, 17 rejected rows, 9 failed rows, 1 gaps row, 1 unknown-hazards row,
9 hazard rows, 10 marker rows, 5 OOXML rows, 11 promotion rows (P1 to P9,
P11, P12), 1 split row, 1 fallback (P10): 90 rows.

`message` for a multi-key verdict is the sentence of the worst key (ties:
first in sorted key order), and `keys` lists every key (the FE shows them
as "Found: JavaScript, page actions" through `rfpFiles.keys.*`, section 8).

### 3.4 Release and re-check conditions at runtime

Static (the Verdict): `may_release = tier in HELD_TIERS and code not in
NEVER_RELEASE and run_status != "expired"`; `may_recheck = tier ==
"rerunnable" and run_status in RUN_TERMINAL_STATUSES - {"expired"}`.

Runtime, checked on every action:

- the run is not `expired` (the prune marks `expired` BEFORE deleting,
  rfp_ingest.py:3072-3081, so `expired` is a safe proxy for "bytes gone");
- `quarantine_path` is set;
- re-check: the object exists (`rfp_ingest_storage.object_exists`, new);
- release: the stored copy downloads, its sha256 equals
  `rfp_ingest_files.sha256`, and its full finding set (the release's own
  scan) equals the set IT reviewed (D2d);
- release: not already released, unless the file is P12 (`released:changed`).

Splitter admission (D2e): `rfp_split.may_stage(file_row) -> bool` =
`not file_row.released_at or rfp_file_verdicts.splitter_ok(file_row)`,
checked inside `_stage_row` (the one function every stager calls,
rfp_split.py:587-662), which raises `StageRefused("released_whole_only")`
BEFORE any byte is parsed; `_start` and `stage_late` also check it before
downloading. A refusal means "promote whole". Rescued (never released)
files reach the splitter only as `verified` (promotion_for admits nothing
else).

Safety argument for letting a RELEASED verified file into the splitter,
which parses with pypdf and renders with pypdfium2 inside the backend
process (pdf_split.py:8-22, 58-64): (1) the bytes are the exact bytes the
sandbox verified (sha256 re-checked at release and again on download,
rfp_create_files.py:676-677); (2) `verified` means the sandboxed child
opened the file and rendered every page with the same PDFium build under
memory, time and disk limits without a failure; (3) the flagged findings
are actions, annotations, embedded files and scripts, which rendering
never executes, and the PDFium build has no JavaScript engine (the child
refuses a V8 build, sandbox/__main__.py:396-410; the backend uses the same
pypdfium2 wheel from the same venv); (4) pypdf is memory safe but NOT
denial-of-service safe: crafted xref or Flate streams have caused infinite
loops and memory exhaustion in pypdf, which a page cap does not bound. The
exposure is the same as for any verified RFP file today; the job lease
bounds a hung worker thread's effect on the queue, not its CPU. A released
file that is not `verified` (R14, X1b), or whose release names an OOXML
reason (O1 to O5), is never handed to the splitter: it is promoted whole.

---

## 4. Data model

### 4.1 `rfp_created_projects.files_skipped` (jsonb, shape change, no DDL)

Written only by the promotion pass's final claimed update (5a step 11).
One entry per key, keyed by sandbox file id (else
`"{harvest_id}:{file_path}"`, and `"{harvest_id}:skipped:{name}"` for a
skipped zip member), covering every harvest of the project, sorted by
`TIERS` index then lowercased `file_path`, capped at `SKIPPED_CAP = 300`
AFTER sorting (the tier counts columns carry the uncapped totals). Shape
(`rfp_file_verdicts.SkippedEntry`):

```json
{
  "key": "6f0c...uuid",
  "sandbox_file_id": "6f0c...uuid",
  "harvest_id": "a1b2...uuid",
  "run_id": "c3d4...uuid",
  "file_path": "E-101 Power Plan.pdf",
  "reason": "hazard:page_actions",
  "tier": "held",
  "code": "hazard:page_actions",
  "message": "The file has actions that run when pages open or close.",
  "keys": ["page_actions"],
  "may_release": true,
  "may_recheck": false,
  "listed": true
}
```

- `reason` is the unchanged `Skip.reason` (legacy readers and the bench
  keep working); `file_path` is `clean_name(entry_name(entry, file_row))`.
- `run_id` is `file_row.run_id` (null at entry level).
- Readers meeting a legacy entry (`{file_path, reason}` only) derive the tier
  with `legacy_tier(reason)`, `listed = true`, `may_*` false.

### 4.2 `files_wait` (jsonb object)

```json
{"runs": ["run uuid", ...], "split_jobs": ["bid_split_jobs uuid", ...]}
```

Written only by the pass's final update: the runs of `checking` entries
(F1, F4, and the runs a re-check just restarted), and the split jobs
holding an in-flight late row. Read by the follow-up sweep (5c). `{}` when
nothing is awaited.

### 4.3 Migration `bdr_be/supabase/migrations/0136_rfp_file_verdicts.sql`

Owned by package A. Exact SQL:

```sql
-- 0136 - RFP Ingestion: file verdicts, re-check and IT release
-- (docs/RFP_FILE_VERDICTS.md section 4). Apply after 0135. DEV ONLY until
-- the release steps in section 11 run.
--
-- A file the sandbox or the promotion gate refuses no longer disappears
-- into a count: every harvested file that is not in the project carries a
-- tier (unsafe, held, not_usable, rerunnable, checking) and a reason on the
-- created-project record, IT is alerted per held finding, a Re-check reruns
-- the sandbox on the files whose check never finished, and an IT Admin can
-- release a held file after inspecting it. This migration adds:
--
--   1. rfp_created_projects: the tier counts, the files already promoted,
--      the runs whose bytes must outlive the 14-day prune, the follow-up
--      and pass-request state the queue sweep reads, the re-check state
--      (manual request, the one automatic retry, the last result) and the
--      IT alert dedupe;
--   2. rfp_harvests: when the split step started waiting on the sandbox and
--      when it gave up (the 60 minute deadline, the /rfp-processing stuck
--      kind);
--   3. rfp_ingest_files: the IT release (who, when, from which project,
--      the reviewed findings and status, the note, the sha256 re-checked at
--      release), the post-download scan of the stored copy, and the failure
--      code a re-check reset;
--   4. bid_split_files: one staged row per sandbox file per split job, so a
--      late staging race can never stage the same bytes twice.
--
-- No new table. Every column lands on a table that already has row level
-- security enabled and forced with no policy (0119, 0123, 0130, the bid
-- splitter's own migration), so the service role stays the only reader and
-- writer. Every statement is idempotent.

-- ── 1. rfp_created_projects ──────────────────────────────────────────────

alter table rfp_created_projects
  add column if not exists files_unsafe            integer  not null default 0,
  add column if not exists files_held              integer  not null default 0,
  add column if not exists files_not_usable        integer  not null default 0,
  add column if not exists files_rerunnable        integer  not null default 0,
  add column if not exists files_checking          integer  not null default 0,
  add column if not exists files_recheckable       integer  not null default 0,
  add column if not exists files_unalerted         integer  not null default 0,
  add column if not exists promoted_ids            uuid[]   not null default '{}',
  add column if not exists hold_run_ids            uuid[]   not null default '{}',
  add column if not exists files_wait              jsonb    not null default '{}'::jsonb,
  add column if not exists files_followup_at       timestamptz,
  add column if not exists files_pass_requested_at timestamptz,
  add column if not exists recheck_runs            uuid[]   not null default '{}',
  add column if not exists recheck_auto            text,
  add column if not exists recheck_auto_at         timestamptz,
  add column if not exists recheck_auto_tries      smallint not null default 0,
  add column if not exists recheck_requested_at    timestamptz,
  add column if not exists recheck_requested_by    uuid references profiles(id) on delete set null,
  add column if not exists recheck_last_at         timestamptz,
  add column if not exists recheck_last_by         uuid references profiles(id) on delete set null,
  add column if not exists recheck_last_result     jsonb,
  add column if not exists it_alerted              jsonb    not null default '{}'::jsonb;

alter table rfp_created_projects drop constraint if exists rfp_created_projects_file_tiers_ck;
alter table rfp_created_projects add constraint rfp_created_projects_file_tiers_ck check (
  files_unsafe >= 0 and files_held >= 0 and files_not_usable >= 0
  and files_rerunnable >= 0 and files_checking >= 0 and files_unalerted >= 0
  and files_recheckable between 0 and files_rerunnable
);

alter table rfp_created_projects drop constraint if exists rfp_created_projects_recheck_auto_ck;
alter table rfp_created_projects add constraint rfp_created_projects_recheck_auto_ck check (
  (recheck_auto is null or recheck_auto in ('scheduled', 'due', 'running', 'done'))
  and recheck_auto_tries between 0 and 100
);

-- recheck_auto_at is set exactly while the automatic re-check is scheduled:
-- it can never outlive the schedule and freeze the button (section 6).
alter table rfp_created_projects drop constraint if exists rfp_created_projects_recheck_auto_at_ck;
alter table rfp_created_projects add constraint rfp_created_projects_recheck_auto_at_ck check (
  (recheck_auto is not distinct from 'scheduled') = (recheck_auto_at is not null)
);

alter table rfp_created_projects drop constraint if exists rfp_created_projects_file_json_ck;
alter table rfp_created_projects add constraint rfp_created_projects_file_json_ck check (
  jsonb_typeof(files_wait) = 'object'
  and jsonb_typeof(it_alerted) = 'object'
  and (recheck_last_result is null or jsonb_typeof(recheck_last_result) = 'object')
);

comment on column rfp_created_projects.files_skipped is
  'Array of per-file verdicts (docs/RFP_FILE_VERDICTS.md 4.1): key, sandbox_file_id, harvest_id, run_id, file_path, reason, tier (unsafe | held | not_usable | rerunnable | checking), code, message, keys, may_release, may_recheck, listed. Every harvest of the project, one entry per sandbox file, sorted by severity, capped at 300. Rows written before 0136 hold {file_path, reason} only.';
comment on column rfp_created_projects.files_unsafe is
  'Listed files in the unsafe tier after the last promotion pass (uncapped). With files_held, files_not_usable, files_rerunnable and files_checking: the badges and callouts, read without the files_skipped array.';
comment on column rfp_created_projects.files_recheckable is
  'Of files_rerunnable, those a re-check can act on (run terminal and not expired) at the last pass. Re-check files is offered only while it is above zero.';
comment on column rfp_created_projects.files_unalerted is
  'Held (unsafe or held tier) files the IT Admins could not be alerted about at the last pass (no active IT Admin). The page then says so instead of "IT has been notified".';
comment on column rfp_created_projects.promoted_ids is
  'Every sandbox file id this project has held a project_files row for (intact, source set or segments), written by the promotion pass. A file in this list whose rows are gone was removed by a person and is never promoted again.';
comment on column rfp_created_projects.hold_run_ids is
  'Sandbox runs whose bytes must outlive the retention prune: the runs of this project''s unsafe, held, rerunnable and checking files, rewritten by every promotion pass. rfp_ingest.prune_expired keeps them until RFP_FILE_HOLD_RETENTION_DAYS after the run was created.';
comment on column rfp_created_projects.files_wait is
  '{"runs": [...], "split_jobs": [...]}: what the last promotion pass is waiting on (active sandbox runs of checking files, split jobs with a late row in flight). The follow-up sweep asks for the next pass once all of them have ended.';
comment on column rfp_created_projects.files_followup_at is
  'When the follow-up sweep next looks at files_wait. Null = nothing awaited. Written by the promotion pass; bumped, or cleared together with setting files_pass_requested_at, by the sweep with a compare-and-set on the old value.';
comment on column rfp_created_projects.files_pass_requested_at is
  'A promotion pass is wanted (a finished wait, an IT release, a re-check request, a linked invitation, an expired held run). Set FIRST by every caller; the pass clears it when it claims the record, and the sweep enqueues a pass for any row where it stays set while no pass holds the record.';
comment on column rfp_created_projects.recheck_runs is
  'Sandbox runs a re-check (manual or automatic) restarted that are still active. Written only by the promotion pass under its claim; the automatic re-check is done when this is empty again.';
comment on column rfp_created_projects.recheck_auto is
  'The one automatic re-check: null (never scheduled), scheduled (recheck_auto_at is when), due (the sweep asked for the pass that runs it), running (the pass restarted runs; waiting for them), done (followed through or refused for good; never scheduled again).';
comment on column rfp_created_projects.recheck_requested_at is
  'A person pressed Re-check files; the next promotion pass runs it and clears this column together with recheck_requested_by.';
comment on column rfp_created_projects.recheck_last_result is
  '{"auto": bool, "runs": [{"run_id", "files"}], "files": n, "gone": n, "refused": [sentence, ...]} of the last re-check a pass ran.';
comment on column rfp_created_projects.it_alerted is
  '{entry key: "tier:code"}: the findings the IT Admins were alerted about (rfp_create.files_held). A pass alerts for a held entry whose key is missing or whose signature changed (an escalation), and records it only after the bell was delivered to at least one IT Admin.';

create index if not exists rfp_created_projects_followup_idx
  on rfp_created_projects (files_followup_at) where files_followup_at is not null;
create index if not exists rfp_created_projects_pass_requested_idx
  on rfp_created_projects (files_pass_requested_at) where files_pass_requested_at is not null;
create index if not exists rfp_created_projects_recheck_auto_idx
  on rfp_created_projects (recheck_auto_at) where recheck_auto = 'scheduled';
create index if not exists rfp_created_projects_hold_runs_gin
  on rfp_created_projects using gin (hold_run_ids);

-- ── 2. rfp_harvests: the sandbox wait stamps ─────────────────────────────

alter table rfp_harvests
  add column if not exists split_sandbox_wait_started_at timestamptz,
  add column if not exists split_sandbox_deadline_at     timestamptz;

comment on column rfp_harvests.split_sandbox_wait_started_at is
  'The first time the split step found the sandbox still checking this harvest''s documents. After RFP_SPLIT_SANDBOX_WAIT_MINUTES the step stops waiting; /rfp-processing flags the row stuck (sandbox_wait) after RFP_PROCESSING_SANDBOX_WAIT_MINUTES.';
comment on column rfp_harvests.split_sandbox_deadline_at is
  'When the split step gave up waiting on the sandbox (written once by compare-and-set): the unowned run is canceled and the bench event recorded only by the winner of that write.';

-- ── 3. rfp_ingest_files: the IT release, the scan, the reset code ────────

alter table rfp_ingest_files
  add column if not exists released_at         timestamptz,
  add column if not exists released_by         uuid references profiles(id) on delete set null,
  add column if not exists released_project_id uuid references projects(id) on delete set null,
  add column if not exists released_tier       text,
  add column if not exists released_code       text,
  add column if not exists released_keys       text[],
  add column if not exists released_status     text,
  add column if not exists released_note       text,
  add column if not exists released_sha256     text,
  add column if not exists promotion_scan      jsonb,
  add column if not exists prior_fail_code     text;

alter table rfp_ingest_files drop constraint if exists rfp_ingest_files_release_ck;
alter table rfp_ingest_files add constraint rfp_ingest_files_release_ck check (
  (released_at is null and released_tier is null and released_code is null
     and released_keys is null and released_status is null
     and released_note is null and released_sha256 is null)
  or (released_at is not null
      and released_tier in ('unsafe', 'held')
      and released_code is not null and length(released_code) <= 200
      and released_keys is not null and cardinality(released_keys) <= 100
      and released_status in ('verified', 'rejected', 'failed')
      and released_note is not null and length(btrim(released_note)) between 1 and 1000
      and released_sha256 ~ '^[0-9a-f]{64}$')
);

alter table rfp_ingest_files drop constraint if exists rfp_ingest_files_scan_ck;
alter table rfp_ingest_files add constraint rfp_ingest_files_scan_ck check (
  (promotion_scan is null or jsonb_typeof(promotion_scan) = 'object')
  and (prior_fail_code is null or length(prior_fail_code) <= 64)
);

comment on column rfp_ingest_files.released_at is
  'An IT Admin released this held file after inspecting it (docs/RFP_FILE_VERDICTS.md 5f). Promotion admits these exact bytes (sha256 = released_sha256) only while the file status equals released_status and every live finding is in released_keys; anything new re-holds it. A release is a verdict on the bytes: every project listing this sandbox file admits it. released_project_id and the audit_log row rfp_file.release name the project it was released from.';
comment on column rfp_ingest_files.released_keys is
  'The full finding set IT reviewed and released: hazard keys, byte markers of the stored copy, "ooxml:<why>", or "<status>:<code>" for a rejected or failed file.';
comment on column rfp_ingest_files.released_sha256 is
  'The sha256 recomputed from the quarantine object at release; equal to sha256 or the release was refused.';
comment on column rfp_ingest_files.promotion_scan is
  '{"sha256", "pdf": "original" | "converted" | null, "marker_keys": [...], "ooxml": "<why>" | null, "at"}: the post-download scan of the stored copy (byte markers as name tokens, the OOXML container verdict), recorded once so a held file is not downloaded on every pass and so tiering and the IT inspect view see every finding. Valid only while sha256 equals the row''s sha256.';
comment on column rfp_ingest_files.prior_fail_code is
  'The reject_code this file carried when a retry last reset it (rfp_ingest.retry_run). A second failed/invalid_output on the same bytes is then treated as a tamper signal (RED).';

create index if not exists rfp_ingest_files_released_idx
  on rfp_ingest_files (released_at) where released_at is not null;

-- ── 4. bid_split_files: one staged row per sandbox file per job ──────────

create unique index if not exists bid_split_files_job_sandbox_uidx
  on bid_split_files (job_id, rfp_sandbox_file_id) where rfp_sandbox_file_id is not null;

notify pgrst, 'reload schema';
```

Pre-check before applying (release step 2, section 11), must return no row:

```sql
select job_id, rfp_sandbox_file_id, count(*) from bid_split_files
 where rfp_sandbox_file_id is not null group by 1, 2 having count(*) > 1;
```

Array rule for every builder: arrays are WRITTEN as JSON lists in update
payloads (PostgREST converts them), always sorted. Arrays are FILTERED
only with `.contains(col, [ids])` / `.ov(...)` (postgrest-py formats
`{a,b}`) or a literal string such as `.neq("hold_run_ids", "{}")`; never
`.eq(col, <python list>)`, which sends the list's Python repr. No design
step here needs a compare-and-set on an array: every array is written only
by the pass under its claim token.

### 4.4 Harvest entries (jsonb, no DDL)

At intake, when `add_upload_file` answers a `rejected` row, both harvesters
(rfp_harvest.py:1566-1569, rfp_portal_ingest.py:2521-2523) now also write:

```python
entry["reject_code"] = frow.get("reject_code")                     # e.g. "polyglot"
entry["sniff_magics"] = list(((frow.get("manifest") or {}).get("sniff") or {})
                             .get("markers_in_head") or [])[:8]
```

The email harvester's zip refusal (rfp_email_harvest.py:404-413) writes
`zip_entry["reject_code"] = {"bomb": "zip_bomb", "not_zip":
"not_zip"}.get(listing.error_kind)` beside the sentence it writes today.

Needed because an all-rejected harvest deletes its run and file rows
(rfp_harvest.py:1589-1596); the entry is then the only record. The
extension rule (D1g) reads the entry's own `file_path` / `file_name`, which
every entry keeps.

### 4.5 Audit rows (existing `notifications.audit`, notifications.py:172-185)

| action | entity | entity_id | payload |
|---|---|---|---|
| `rfp_file.release` | `rfp_ingest_file` | sandbox file id | `{project_id, harvest_id, file_path, tier, code, keys, status, note, sha256, run_id, replaced_release_at}` |
| `rfp_file.inspect` | `rfp_ingest_file` | sandbox file id | `{project_id, tier, code}` |
| `rfp_created.recheck_request` | `rfp_created_project` | project id | `{files}` (route, manual) |
| `rfp_created.recheck` | `rfp_created_project` | project id | `{auto, requested_by, runs: [{run_id, files}], gone, refused: [sentence]}` (the pass) |
| `rfp_created.retry_files` | (unchanged) | | |

---

## 5. Flows

Module owners: A = `rfp_file_verdicts.py`, `rfp_create_files.py`,
`rfp_create.py`, `notification_email.py`, `notifications.py`, migration.
B = `rfp_split.py`, `rfp_ingest.py`, `rfp_ingest_storage.py`,
`llm_queue.py`, `pdf_split.py`. C = `rfp_file_rescue.py`, the routers
(including `bid_splitter.py`), `schemas.py`, `rfp_processing.py`. E = the
three harvesters. Step 0 = `config.py` (section 9).

### 5a. Promotion pass (`rfp_create_files.execute`, package A)

New and changed names in `rfp_create_files.py` (final):

```python
SKIPPED_CAP = 300                                   # replaces _SKIPPED_CAP = 200 (:129)
_FILE_SELECT = ("id, run_id, status, reject_code, error, hazards, quarantine_path, "
                "source_format, converted_path, filename, size_bytes, sha256, manifest, "
                "promotion_scan, prior_fail_code, released_at, released_by, released_status, "
                "released_keys, released_sha256, released_project_id")   # (:133-136)
RELEASABLE_STATUSES = (protocol.STATUS_VERIFIED, protocol.STATUS_REJECTED,
                       protocol.STATUS_FAILED)
OOXML_REASONS = frozenset({"too_many_members", "vba_project", "embedding", "binary_part",
                           "external_link", "member_too_large", "dde", "macro_enabled",
                           "bad_zip"})                       # plus the "external_rel:<word>" family
REASON_SPLIT_RUNNING = "split:running"
REASON_REMOVED = "promotion:removed"
REASON_RELEASE_CHANGED = "released:changed"
ZIP_MEMBER_REASONS = frozenset({"unsafe_name", "encrypted", "nested_zip", "empty", "too_large"})
_AUTO_RECHECK_RETRY_SECONDS = 300    # a transient refusal reschedules the automatic re-check
_AUTO_RECHECK_MAX_TRIES = 6

@dataclass(frozen=True)
class Promote:                                      # (:165-179) one new field
    ...
    released_keys: frozenset[str] | None = None     # an IT release: findings admitted

@dataclass(frozen=True)
class FileScan:
    sha256: str
    pdf: str | None                   # "original" | "converted" | None
    marker_keys: tuple[str, ...]
    ooxml: str | None
    def to_json(self, now: datetime) -> dict: ...

@dataclass(frozen=True)
class LiveFile:
    harvest_id: str
    entry: dict
    file_row: dict | None
    run_status: str | None
    verdict: "Verdict | None"         # None: the file would be promoted now

def run_statuses(sb, run_ids: Iterable[str]) -> dict[str, str]: ...
def project_harvests(sb, project_id: str, record: Mapping[str, Any] | None = None,
                     harvest_id: str | None = None) -> list[dict]: ...
def annotate_entries(sb, entries: list[dict]) -> None: ...
def session_frozen(sb, test_session_id: str | None) -> bool: ...
def scan_file(file_row: dict, scratch: Path, settings: Settings) -> FileScan | Skip: ...
def store_scan(sb, file_id: str, scan: FileScan) -> None: ...
def live_file(sb, project_id: str, sandbox_file_id: str) -> LiveFile | None: ...
def request_pass(sb, project_id: str) -> None: ...
def request_pass_for_runs(sb, run_ids: Iterable[str], *, exclude_project_id: str | None = None) -> int: ...
def enqueue_pass(sb, project_id: str, *, harvest_id: str | None = None,
                 created_by: str | None = None,
                 settings: Settings | None = None) -> dict | None: ...
def _claim(sb, project_id: str, token: str, settings: Settings) -> dict | None: ...   # now returns the row
def _promote_one(sb, project_id, harvest_id, entry, file_row, decision, data, *,
                 category: str | None = None, note: str | None = None) -> str | None: ...
```

- `promotion_for` (:286-320) gains one branch right after the
  `file_row is None` check, for `file_row.released_at` and
  `released_sha256` set:
  1. `file_row.status != released_status` -> `Skip(REASON_RELEASE_CHANGED)`.
  2. `status == verified` and the hazard keys (or `hazards_unknown`) are not
     all in `released_keys` -> `Skip(REASON_RELEASE_CHANGED)`.
  3. `status in (rejected, failed)` and `f"{status}:{reject_code}"` not in
     `released_keys` -> `Skip(REASON_RELEASE_CHANGED)`.
  4. No `quarantine_path` -> `Skip(REASON_NO_SOURCE)`.
  5. By format: `pdf` -> `Promote(QUARANTINE_BUCKET, quarantine_path, name,
     "application/pdf", True, expected_sha=released_sha256,
     released_keys=keys)`; `docx` / `xlsx` -> the same with the office
     content type and `ooxml_scan=True`; `doc` / `xls` ->
     `converted_promotion(file_row, name, fmt)` with `released_keys=keys`
     (never the legacy original). `Promote.note` for the release is added
     by the pass (step 5.10).
  Every other file keeps today's gates.
- `_fetch_verified` (:667-682): when `decision.released_keys` is set, a
  marker key NOT in `released_keys` returns `Skip(REASON_RELEASE_CHANGED)`;
  markers inside the set are admitted. `_fetch_entry` (:685-708): when
  `decision.released_keys` is set and the OOXML scan returns `why`,
  `f"ooxml:{why}" in released_keys` admits the original; otherwise the
  existing fallback to the converted PDF runs, whose markers are checked
  against `released_keys` in turn.
- `scan_file(file_row, scratch, settings)`: downloads (max
  `rfp_ingest_max_file_bytes`) and checks, never promotes. Original from
  the quarantine bucket, sha256 against `file_row.sha256` (mismatch:
  `Skip(REASON_SHA_MISMATCH)`; missing: `Skip(REASON_MISSING_IN_STORAGE)`);
  `pdf` -> `pdf_marker_keys(original)`, `pdf = "original"`; `docx` /
  `xlsx` -> `ooxml = ooxml_container_verdict(original)`, and with a
  `converted_path` the converted PDF (conversion digest checked, mismatch
  `Skip(REASON_CONVERTED_SHA_MISMATCH)`) gives `marker_keys`, `pdf =
  "converted"`; `doc` / `xls` -> the converted PDF's markers. Storage
  trouble raises `RfpCreateFilesTransient`. `store_scan` writes
  `rfp_ingest_files.promotion_scan = scan.to_json(now)` for that id.
- `live_file(sb, project_id, sid)`: the entry naming `sid` in
  `project_harvests` (first harvest wins), its file row (`_file_rows`), its
  run status, and `classify(promotion_for(...).reason, ...)` when the gate
  skips it (no download; the stored `promotion_scan` counts). None when no
  entry names it. Used by C's release, inspect and files view.
- `run_statuses`: one `select id, status from rfp_ingest_runs where id in
  (...)` per 200 ids.
- `project_harvests`: the record's `harvest_id` first, then every
  `rfp_harvests` row with `project_id = project_id` ordered by
  `created_at`, then the payload `harvest_id`; deduped by id; columns `id,
  files, split_job_id, split_status, sandbox_run_id, project_id,
  test_session_id, data, method, created_at` (never `raw`).
- `annotate_entries` (the pre-creation card, DF4): for every dict entry,
  `entry["verdict"] = None` when `promotion_for` promotes AND neither a
  valid scan nor `provisional_red_markers` finds anything; else
  `{"tier", "code", "message", "provisional"}`: `classify` when the gate
  skips; `marker:<keys>` from a valid `promotion_scan`
  (`provisional: false`); or, with no valid scan, the worst of
  `provisional_red_markers` (`provisional: true`, message of that key). No
  download. Loads file rows and run statuses in two queries. Never raises
  (logs and leaves entries untouched).
- `session_frozen(sb, test_session_id)`: False without an id; else True
  unless `rfp_test.active_session(sb)` (rfp_test.py:243) is that session
  (DF5, mirroring the pollers' frozen rule, rfp_test.py:353-357).
- `retryable` (:471-482) loses the "complete with a non-empty
  files_skipped" clause: "Retry documents" is for promotion job failures
  only (DF1). The existing test at tests/test_rfp_create_files.py:937 flips.
- `request_pass`: `update files_pass_requested_at = now() where project_id
  = X and files_pass_requested_at is null`. Never raises (logs).
- `request_pass_for_runs(sb, run_ids, exclude_project_id)`: for each run,
  `select project_id from rfp_created_projects where hold_run_ids @>
  '{run}'` (`.contains("hold_run_ids", [run_id])`), minus the excluded
  project; `request_pass` each; returns how many. Never raises.
- `enqueue_pass` (never raises; the marker makes the pass durable):
  1. `request_pass(sb, project_id)` FIRST.
  2. Load the record (None when gone -> None). `prior = files_status`.
  3. `prior == running` and not `claim_is_stale` (:453-468) -> None (the
     running pass's successor comes from the marker, 5c step 3).
  4. `prior == pending` and `active_job(project_id)` (:434-435) is not None
     -> None (that job claims and reads fresh state).
  5. `mark_pending(sb, project_id, prior)` (:485-503); False -> None.
  6. `llm_queue.enqueue(JOB_TYPE, target_id=project_id,
     project_id=project_id, payload={"project_id": project_id,
     **({"harvest_id": harvest_id} if harvest_id else {})},
     created_by=created_by, priority=s.rfp_create_files_queue_priority,
     settings=s, raise_on_active=True)`; `JobAlreadyActive` or any other
     exception: `unmark_pending`, log, None. Returns the job.

`execute(project_id, harvest_id=None)` steps (replacing :813-951). Every
download step calls `_renew()` (:595-597) first, as today.

1. Claim FIRST: `record = _claim(sb, project_id, token, s)`; `_claim`
   (:546-568) additionally sets `files_pass_requested_at = null` in the
   same update and returns the updated row. None: `_load_record` (:438-440)
   missing -> `RfpCreateFilesPermanent`, else `RfpCreateFilesTransient`
   (unchanged behavior). Everything below reads `record`, the claimed row
   (never a pre-claim read).
2. `s`, `now`, `tz = s.display_timezone`. `previous = {e["key"]: e for e in
   record.files_skipped if dict with a "key"}`. `promoted_before =
   set(record.promoted_ids)`. `it_alerted = dict(record.it_alerted)`.
   `frozen = session_frozen(sb, record.test_session_id)`. `locked =
   files.handoff_locked(project_id)` (files.py:187-217, imported inside the
   function like rfp_split.py:1285), re-read before every landing decision
   while False (the lock only ever turns on). `split_on =
   rfp_split.enabled(s) and s.llm_queue_enabled` (rfp_split.py:200-203).
   `mode = "auto" if record.recheck_auto == "due" else ("manual" if
   record.recheck_requested_at else None)`.
3. `harvests = project_harvests(sb, project_id, record, harvest_id)`.
   `done_ids = _already_promoted(sb, project_id)` (:620-628) and `existing
   = rfp_split.project_rows(sb, project_id)` (rfp_split.py:968-979).
4. For each harvest `h`: `entries` = its dict entries, plus one synthetic
   entry per `h.data.attachments.skipped` row whose `reason` is in
   `ZIP_MEMBER_REASONS` (`{"status": "skipped_member", "reject_code":
   reason, "file_path": name}`, key `f"{h.id}:skipped:{name}"`);
   `file_rows = _file_rows(sb, ids)`; `runs = run_statuses(sb,
   {row.run_id})`; `split_rows = rfp_split.split_rows_for_job(sb,
   h.split_job_id)` (rfp_split.py:955-965; the record's `split_job_id` only
   for the record's own harvest, as today :859); `split_ran = bool(
   h.split_job_id) or h.split_status in ("running", "complete",
   "failed")`; `late: list[(entry, file_row)] = []`.
5. For each entry (index `i`), with `sid`, `file_row`, `key =
   entry_key(entry, h.id)`, `name = entry_name(entry, file_row)`,
   `split_file, segments = split_rows.get(sid, (None, []))`:
   1. `handled` dedupe per sid (:873-876).
   2. Present: `sid in done_ids`, or `split_file` is set and
      `rfp_split.rows_for_file(existing, split_file)` is non-empty. Then add
      `sid` to `promoted_now`; when `previous[key]` had a tier in
      `OPEN_TIERS` (it was not on the project at the last pass, so the
      splitter's resync filed it since), add its rows in `DRAWING_CATEGORIES`
      (source set excluded) to `drawings`; continue.
   3. `sid in promoted_before` and not present -> record `classify(
      REASON_REMOVED, ...)` (P11); continue (B5).
   4. `decision = promotion_for(entry, file_row)`. On `Skip`:
      - AMBER escalation check: when the reason is `hazard:*`, the file is
        `verified` and not released, `worst(HAZARD_TIERS[k] for the hazard
        keys) == "held"` and `scan_of(file_row)` is None: `scan =
        scan_file(file_row, scratch, s)`; a `FileScan` -> `store_scan`, and
        `file_row = {**file_row, "promotion_scan": scan.to_json(now)}`; a
        `Skip` -> classify that Skip's reason instead (P1, P2, P3).
      - Record `classify(reason, entry=entry, file_row=file_row,
        run_status=runs.get(run_id))`; continue.
   5. `split_file.status in ("pending", "running")` -> record
      `classify(REASON_SPLIT_RUNNING, ...)` (S1), add `split_file.job_id` to
      `wait_split_jobs`; continue. (After step 5.2, so a person re-running a
      filed document in the splitter never lists it as missing.)
   6. Not `locked`, `split_file.status == "done"` with segments, and `sid`
      not in `promoted_before`: `rfp_split.promote_split_file(...)` as today
      (:878-892) plus `release_note=release_note(file_row.released_at, tz)`
      when released; add `sid` to `promoted_now`; its `result.skipped`
      entries become P9 verdicts; continue.
   7. Sticky scan (no download): with `scan = scan_of(file_row)`: markers
      not admitted (not released, or not all in `released_keys`) ->
      record `classify("marker:" + ",".join(markers), ...)`; `ooxml` set,
      no `converted_path`, not admitted -> `classify("ooxml:" + why)`;
      `previous[key].code` in (`promotion:changed_since_verification`,
      `promotion:converted_sha_mismatch`) and not released -> reuse it
      (fresh message); continue.
   8. Re-read `locked` while False. When `locked`: fetch (`_fetch_entry`,
      :685-708); on `Skip` classify it (and `store_scan` for a `marker:` /
      `ooxml:` Skip, built from the reason); else `_promote_one(...,
      category="additional", note=handoff_note(kind, when=released_at or
      now, tz=tz))` with `kind = "released"` when released, `"rescued"`
      when `previous[key]` had a tier in `OPEN_TIERS`, else `"added"`
      (D4b); count it under that kind (only when a row was inserted); add
      to `promoted_now`; continue. (D4, D4a.)
   9. Late: `split_on and split_ran and split_file is None and
      rfp_split.may_stage(file_row) and splitter_ok(file_row)` -> append
      `(entry, file_row)` to `late`; continue. (Independent of `previous`: a
      file still pending when the split step staged, or released since,
      goes to the splitter.)
   10. Otherwise fetch and promote whole exactly as today (:895-911), with
       `note=release_note(file_row.released_at, tz)` when released (joined
       with the converted-PDF note by "; " when both apply); add to
       `promoted_now`. A fetch `Skip` is classified, and a `marker:` /
       `ooxml:` Skip is stored with `store_scan` (built from the reason, the
       file's sha256 and the decision's bucket) so the next pass does not
       download it again.
6. After the harvest's loop, when `late`: `result = rfp_split.stage_late(sb,
   h, project_id, late, settings=s, renew=_renew)` (package B, 5c). For
   each sid in `result.staged`: record verdict S1 and add `result.job_id`
   to `wait_split_jobs`. For each `(sid, reason)` in `result.refused`: when
   `reason in (rfp_split.REFUSED_OVER_CAP, rfp_split.REFUSED_WHOLE_ONLY)`,
   promote that entry whole now (step 5.10); else classify `reason`. When
   `result.created_job` and `h.id == record.harvest_id`, the final update
   also writes `split_job_id = result.job_id`; when anything was staged for
   the record's harvest, the final update writes `split_status =
   "running"`.
7. Re-check (D3d), when `mode`: `candidates = [RecheckCandidate(sid,
   run_id, quarantine_path)` for recorded verdicts with `tier ==
   "rerunnable" and may_recheck]`; `outcome =
   rfp_file_rescue.run_recheck(sb, project_id, candidates, actor_id=(
   record.recheck_requested_by if mode == "manual" else None),
   settings=s, renew=_renew)` (package C, 5d). Every sid in
   `outcome.started` is re-recorded as F1 (`run_status = "pending"`);
   every sid in `outcome.gone` as P3. Audit `rfp_created.recheck`; bench
   event `create.recheck` (or `create.recheck_refused` when nothing
   started).
8. Record's split status refresh: when the record's `split_status ==
   "running"` and `split_job_id`'s job is terminal (`done` /
   `done_with_errors` -> `complete`, `failed` -> `failed`) and nothing of
   the record's harvest is in `wait_split_jobs`, the final update writes
   that value (the /rfp-created "Splitting documents" badge,
   rfpCreated.ts:184-189, turns off).
9. Assemble: every recorded verdict becomes `to_skipped(verdict,
   reason=..., entry=..., file_row=..., harvest_id=h.id, name=name)`;
   dedupe by key (first wins); `tier_counts = counts(entries)`;
   `recheckable = count of listed rerunnable entries with may_recheck`;
   `skipped = sort_and_cap(entries, SKIPPED_CAP)`; `hold = sorted({e.run_id
   for e in entries if e.tier in OPEN_TIERS and e.run_id and
   runs.get(e.run_id) != "expired"})`; `wait = {"runs": sorted(runs of
   checking entries from F1 / F4 whose run is active, plus the started
   runs), "split_jobs": sorted(wait_split_jobs)}` (empty lists dropped;
   `{}` when both are empty); `promoted_ids = sorted(promoted_before |
   promoted_now)`; `recheck_runs = sorted(r for r in set(
   record.recheck_runs) | started_runs if status of r is active)` (one
   `run_statuses` read); `promoted = rfp_split.count_documents(sb,
   project_id)` (rfp_split.py:1206-1210; always).
10. IT alert (D2), under the claim, BEFORE the final write: `held = [e for
    e in entries if e.tier in HELD_TIERS and e.listed]`; `new_alert = [e
    for e in held if it_alerted.get(e.key) != alert_signature(e)]`. When
    `new_alert` and not `frozen`: `n = notify_role(Role.IT_ADMIN,
    project_id, "rfp_create.files_held", held_alert_message(label,
    new_alert), mirror_email=True, metadata=...)` (section 7; `notify_role`
    now returns the number of bells written). `n > 0`: `it_alerted[e.key] =
    alert_signature(e)` for every `new_alert` entry. An exception is logged
    and treated as `n = 0`. `unalerted = 0 if frozen else len([e for e in
    held if it_alerted.get(e.key) != alert_signature(e)])`. (Notify first,
    then record: a crash in between repeats one alert; a lost alert is
    never recorded as sent.)
11. ONE final `_update_claimed` (:571-580, fenced on the claim token):
    `files_status = complete`, `files_promoted`, `files_skipped`,
    `files_error = null`, the claim cleared, `files_unsafe ...
    files_checking` from `tier_counts`, `files_recheckable`,
    `files_unalerted = unalerted`, `promoted_ids`, `hold_run_ids = hold`,
    `files_wait = wait`, `files_followup_at = now +
    rfp_file_followup_seconds if wait else null`, `it_alerted`,
    `recheck_runs`, the split fields of steps 6 and 8, and the re-check
    transition, first match wins:
    - `mode == "auto"`: started -> `recheck_auto = 'running'`; nothing
      started, `outcome.transient` and `record.recheck_auto_tries + 1 <
      _AUTO_RECHECK_MAX_TRIES` -> `'scheduled'`, `recheck_auto_at = now +
      _AUTO_RECHECK_RETRY_SECONDS`, `recheck_auto_tries + 1`; otherwise
      `'done'` (`auto_done = True`).
    - `record.recheck_auto == 'running'` and the new `recheck_runs` is
      empty -> `'done'` (`auto_done = True`).
    - `record.recheck_auto is None`, `recheckable > 0` and
      `s.rfp_file_recheck_auto_minutes > 0` -> `'scheduled'`,
      `recheck_auto_at = now + N minutes` (once per project, ever).
    - `mode` set: `recheck_last_at = now`, `recheck_last_by` (the requester
      or null), `recheck_last_result` (`{"auto", "runs", "files", "gone",
      "refused"}`); `mode == "manual"` also writes `recheck_requested_at =
      null, recheck_requested_by = null`. A request that arrived during
      this pass is not in the claimed row, so its columns are not written
      and it survives for the next pass (its `files_pass_requested_at`
      marker was set after the claim).
    A lost claim returns (as today :945-947).
12. After the write, best effort, each in its own try/except (log only),
    all bells skipped when `frozen`:
    1. D3c: when `auto_done` and `tier_counts["rerunnable"] > 0`:
       `notify_role(Role.ESTIMATING_ADMIN, project_id,
       "rfp_create.files_recheck_needed", recheck_needed_message(label, n),
       mirror_email=True, metadata=...)`.
    2. Hand-off landing (D4): when any `additional` row was inserted:
       `notify_role(Role.ESTIMATING_ADMIN, project_id,
       "rfp_create.files_added_after_handoff",
       added_after_handoff_message(label, counts_by_kind),
       mirror_email=True, metadata=...)`.
    3. Dismissals: when `files_unsafe + files_held == 0` now and was `> 0`
       before, `dismiss_notifications(project_id=project_id,
       types=["rfp_create.files_held"])`; when `files_rerunnable == 0`,
       dismiss `rfp_create.files_recheck_needed` (notifications.py:113-170).
    4. `_notify_drawings(sb, project_id, drawings)` (:783-810, unchanged;
       `drawings` now includes late landings filed by the resync, step 5.2).
    5. Bench events (5j).

`rfp_create.py` (package A):
- `_entries_with_files` (:1084-1088) becomes "the harvest has at least one
  dict entry", so a harvest whose every file was rejected at intake still
  gets a promotion pass (classification, alerts).
- `_link_files_job`'s busy branch (:1264-1273) calls
  `rfp_create_files.request_pass(sb, project_id)` before `_note_error`, and
  `_MSG_FILES_BUSY` (:149-152) becomes "This invitation's documents are
  queued: the project is filing another invitation's documents first, and
  these follow automatically." The pass finds the harvest through
  `rfp_harvests.project_id`, which `_attach_harvest` (:1124-1152, via
  `_release_claim` :660-673) sets before `_link_files_job` runs
  (:1321-1322).
- The `rfp_create_files` job is claimed only while `rfp_ingest_enabled`
  (llm_queue.py:521-524), unchanged.

### 5b. Split step sandbox deadline (`rfp_split.advance`, package B)

New in `rfp_split.py`:

```python
REFUSED_OVER_CAP = "over_cap"
REFUSED_WHOLE_ONLY = "released_whole_only"
_MSG_SANDBOX_DEADLINE = "The sandbox did not finish in time; the project is created with the files that passed."

class StageRefused(ValueError): ...
def may_stage(file_row: Mapping[str, Any]) -> bool: ...
def context_for_harvest(harvest: dict) -> dict: ...
def sandbox_wait_since(sb, harvest: dict) -> datetime: ...
def _sandbox_deadline(sb, harvest: dict, pending: tuple[int, int], settings: Settings,
                      session_id: str | None, rfp_email_id: str | None) -> None: ...
```

New in `rfp_ingest.py`:

```python
_UNOWNED_GRACE_SECONDS = 300
def cancel_if_unowned(run_id: str, *, grace_seconds: int = _UNOWNED_GRACE_SECONDS) -> bool: ...
```

Steps, inside the `SPLIT_NONE` branch of `advance` (rfp_split.py:512-514):

1. `pending = sandbox_busy(sb, harvest, entries)` (unchanged,
   rfp_split.py:420-445).
2. When `pending`: `since = sandbox_wait_since(sb, harvest)`: CAS
   `update rfp_harvests set split_sandbox_wait_started_at = now where id = X
   and split_sandbox_wait_started_at is null`, returning the stored value
   (the row's existing value when the CAS found it set; a re-read when
   neither is available).
3. When `now - since < rfp_split_sandbox_wait_minutes`: return
   `_wait_for_sandbox(...)` (unchanged, :448-462).
4. Else `_sandbox_deadline(...)`: CAS `split_sandbox_deadline_at = now
   where id = X and split_sandbox_deadline_at is null`; ONLY the CAS winner
   calls `rfp_ingest.cancel_if_unowned(run_id)` when `harvest.sandbox_run_id`
   names a run in `protocol.RUN_ACTIVE_STATUSES`, and records the bench
   event `split.sandbox_deadline` (5j). Then, winner or not, FALL THROUGH to
   `model_away` and the claim exactly as if the sandbox had answered.
   `_start` stages what promotes (rows still pending are refused by
   `promotion_for` as today); the create step follows; the promotion pass
   classifies unfinished files `rerunnable` (run canceled, F2) or
   `checking` (run still owned, F1, D3a), and stages them late once they
   verify (5a step 5.9).
5. `rfp_ingest.cancel_if_unowned(run_id, grace_seconds)`: load the run;
   when its status is in `_RUN_CANCELABLE_FROM` (rfp_ingest.py:176),
   `llm_queue.active_job(llm_queue.JOB_RFP_INGEST, run_id)` is None
   (llm_queue.py:401-415) AND the run's `updated_at` is older than
   `grace_seconds` (the window between a retry's run CAS and its enqueue is
   never mistaken for a stranded run): `cancel_run(run_id)`
   (rfp_ingest.py:2398-2418), which marks `canceled` and hands `running`
   files back as `pending`/`interrupted` (rfp_ingest.py:596-617), making the
   run retryable (`_RUN_RETRYABLE` includes canceled, :178-180). Returns True
   when it canceled; `RfpIngestPermanent` (the run moved) is False. Never
   raises. Pipeline runs have `created_by` null, so no bell (:637-639).

`may_stage(file_row)`: section 3.4. `_stage_row` (rfp_split.py:587-662)
raises `StageRefused(REFUSED_WHOLE_ONLY)` first thing when `not
may_stage(file_row)`; `_start` (rfp_split.py:679-787) checks `may_stage`
before `fetch_entry` and records `{"staged": False, "reason":
REFUSED_WHOLE_ONLY}` in `staged_names` (the pass then promotes it whole).

`context_for_harvest(harvest)`: `{"subject": None, "project_name":
(harvest.data or {}).get("project_name"), "invitation_method":
harvest.method}` (the shape of `context_for_email`, rfp_split.py:269-281).

Both ingest modules reach this through `advance` (email
rfp_email_ingest.py:2816-2843, portal rfp_portal_ingest.py:1891-1911); no
change there.

### 5c. Follow-through: runs and late splits that finish after creation

One mechanism covers the Re-check, the automatic retry, the deadline path,
an IT release and the split-flags-off case (create ran while files were
`checking`): the pass writes what it waits on (`files_wait`,
`files_followup_at`); everything that wants a pass sets
`files_pass_requested_at` FIRST (`request_pass`, directly or through
`enqueue_pass`); ONE throttled sweep turns "everything awaited has ended"
and "a pass was asked for" into a pass. There is no hook inside
`rfp_ingest._mark` (the sandbox's CAS path stays untouched).

**The sweep hook (package B, `llm_queue.py`).** `_sweep`
(llm_queue.py:817-889) runs on every tick of `worker_loop` in every worker,
BEFORE the claim tick (llm_queue.py:1017-1019), so the follow-up must not
run inline. Add, after the lease-expiry block and before the hourly prune:

```python
_last_rfp_followup: float = 0.0
_rfp_followup_lock = threading.Lock()

    global _last_rfp_followup
    if s.rfp_ingest_enabled and (
        time.monotonic() - _last_rfp_followup >= s.rfp_file_followup_seconds
    ) and _rfp_followup_lock.acquire(blocking=False):
        _last_rfp_followup = time.monotonic()
        threading.Thread(target=_rfp_followup, args=(s,), name="rfp-file-followup",
                         daemon=True).start()

def _rfp_followup(s: Settings) -> None:
    try:
        from app.services import rfp_file_rescue

        rfp_file_rescue.followup_tick(s)
    except Exception:  # noqa: BLE001 - follow-ups must never wedge the worker
        logger.exception("llm queue: rfp file follow-up failed")
    finally:
        _rfp_followup_lock.release()
```

The sweep only starts the thread (never waits); at most one follow-up runs
per worker at a time. The worker loop only runs with `llm_queue_enabled`
(main.py:138-150), which is why re-check and release answer 503 without
the queue (section 6).

**`rfp_file_rescue.followup_tick(settings: Settings | None = None) ->
dict[str, int]`** (package C). Returns counters for logs and tests. Reads,
compare-and-set writes on scalar columns, and `enqueue_pass` calls only
(no downloads, no sandbox work). Every step skips rows for which
`rfp_create_files.session_frozen(sb, row.test_session_id)` is True (DF5).
Both uvicorn workers running it is harmless.

1. Automatic re-checks due (D3): `select project_id, recheck_auto_at,
   test_session_id from rfp_created_projects where recheck_auto =
   'scheduled' and recheck_auto_at <= now order by recheck_auto_at limit
   20`. For each: CAS `recheck_auto = 'due', recheck_auto_at = null,
   files_pass_requested_at = now` (`eq recheck_auto 'scheduled'`, `lte
   recheck_auto_at now`); on a win `rfp_create_files.enqueue_pass(sb,
   project_id)` and bench event `create.followup` (reason `auto_recheck`).
   The marker and the state change are one write, so a failed enqueue is
   retried by step 3.
2. Pass waits due: `select project_id, files_wait, files_followup_at,
   test_session_id ... where files_followup_at <= now limit 50`. For each:
   every run in `files_wait.runs` still in `RUN_ACTIVE_STATUSES` is first
   given `rfp_ingest.cancel_if_unowned(run_id)` (a stranded run, such as a
   re-check whose enqueue failed or a split-flags-off run with no job,
   becomes canceled and its files rerunnable); `waiting =` any run still
   active OR any job in `files_wait.split_jobs` has
   `rfp_split.late_job_state(sb, job_id) == "running"`. When waiting: CAS
   `files_followup_at = now + rfp_file_followup_seconds` (`eq
   files_followup_at` the value read). Else CAS `files_followup_at = null,
   files_pass_requested_at = now` (same fence, one write), then
   `enqueue_pass`; bench event `create.followup` (reason `files_wait`).
3. Pass requests: `select project_id, files_pass_requested_at,
   files_status, files_claimed_at, test_session_id ... where
   files_pass_requested_at is not null limit 50`. Skip a row whose
   `files_status` is `running` and not stale, or `pending` with an active
   `rfp_create_files` job. Else `enqueue_pass(sb, project_id)` (bench event
   `create.followup`, reason `pass_requested`, only when a job was
   enqueued).

**`rfp_split.late_job_state(sb, job_id: str) -> Literal["running", "done",
"missing"]`** (package B): `missing` when the job row is gone; `running`
while `status == "processing"`; when terminal, the existing outage rule
applies unchanged (`_outage_failures`, rfp_split.py:879-903): model still
away (`model_away`, :386-402) -> `running`; model back ->
`_requeue_after_outage` (:906-939, with `harvest = {"id":
job.rfp_harvest_id}`, session and email ids None) -> `running`; otherwise
`done`.

**`rfp_split.stage_late(sb, harvest: dict, project_id: str, items:
list[tuple[dict, dict]], *, settings: Settings | None = None, context: dict |
None = None, renew: Callable[[], None] | None = None) -> LateStage`**
(package B):

```python
@dataclass(frozen=True)
class LateStage:
    job_id: str | None
    created_job: bool
    staged: tuple[str, ...]          # sandbox file ids staged now or found already staged
    refused: dict[str, str]          # sandbox file id -> Skip reason, REFUSED_OVER_CAP or REFUSED_WHOLE_ONLY
```

1. `job = _job(sb, harvest.split_job_id)` (rfp_split.py:793-797) when it
   exists with `source == "rfp"` (a zero-file `failed` job from `no_files`,
   :751-756, is reused too). Else insert a job exactly like `_start`
   (:689-700) plus `project_id`, then CAS `rfp_harvests.split_job_id =
   job_id where id = harvest.id and split_job_id is null` (on a lost CAS,
   discard the new job with `_discard_job`, :665-676, and use the winner's).
   Set `bid_split_jobs.project_id = project_id` where null. The harvest's
   `split_status` is NOT touched (it stays terminal so `advance` never
   restages).
2. Already staged: `select rfp_sandbox_file_id from bid_split_files where
   job_id in (jobs of this harvest: bid_split_jobs where rfp_harvest_id =
   harvest.id) and rfp_sandbox_file_id in (sids)`; those sids go to
   `staged` without work.
3. `may_stage` false -> `refused[sid] = REFUSED_WHOLE_ONLY` (no download).
   Cap: `room = bid_split_max_files_per_job - job.file_count`
   (config.py:177); items beyond it go to `refused` as `REFUSED_OVER_CAP`
   (the promotion pass promotes them whole).
4. Per item, in a scratch dir (`_scratch_dir`, :555-559): `renew()` when
   given (it raises `RfpCreateFilesTransient` on a lost lease, which ends
   the pass cleanly; `llm_queue_lease_seconds` is 900, config.py:218, and a
   whole-run rescue can stage 250 files of up to 450 MB); `decision =
   rcf.promotion_for(entry, file_row)`, `rcf.fetch_entry(...)` (Skip ->
   `refused`), then `_stage_row(sb, job_id, entry, file_row, decision,
   data, name, context or context_for_harvest(harvest), settings, dest)`
   (:587-662) and the `llm_queue.enqueue(JOB_BID_SPLIT, ...)` block of
   `_start` (:729-740). A unique violation from the new
   `bid_split_files_job_sandbox_uidx` (`_stage_row` already deletes its
   object and re-raises, :655-662) means "already staged": the sid goes to
   `staged`. `StageRefused` -> `refused[sid] = REFUSED_WHOLE_ONLY`.
5. `update bid_split_jobs set file_count = (count of its files)`, then
   `bid_split.refresh_job(job_id)` (bid_split.py:287-333), which flips a
   `done` / `failed` job back to `processing` while a row is pending.
6. Bench event `split.late_staged` (5j). Returns the `LateStage`.

When a late row finishes `done`, the worker's `_after_done` ->
`resync_after_run` -> `resync_project_files` files it on the project
(bid_split.py:1379-1390, rfp_split.py:1223-1297) unless the hand-off has
sent. Package B also changes `resync_project_files` to prefer
`job.rfp_harvest_id` over the record's `harvest_id` when choosing the
harvest (rfp_split.py:1237), so a linked harvest's late job finds its own
entries, and to pass `release_note=` (below) when the file row is released.
The next promotion pass (the sweep, step 2, once `late_job_state` says
`done`) refreshes `files_skipped`, the counts, `promoted_ids` and the split
status, rings the drawing bell for what the resync filed (5a step 5.2), and
promotes a late row that FAILED whole (5a step 5.10: the split file exists
but is not `done`, so it falls through, exactly today's fallback,
rfp_create_files.py:877-911).

`promote_split_file` (rfp_split.py:1097-1203) gains `release_note: str |
None = None`: when set, it is appended ("; ") to every segment row's note
(`_promote_segment`, :1048-1063), the source-set note (:1149-1164) and the
intact row's note (:1196-1197), so a released file's provenance travels
with every row it lands as (D2c).

**Hand uploads into `rfp` jobs (package C, `routers/bid_splitter.py`).**
`upload_job_file` (bid_splitter.py:250-290) answers 409 "Files cannot be
added by hand to a split job made from an RFP invitation; upload them to
the project instead." when `job.source == "rfp"`, right after
`_job_or_404` (:66-72). This keeps the invariant the splitter safety
argument relies on: an `rfp` job holds only sandbox-admitted bytes.

Idempotency and races: one promotion job per project (the queue's one
active job per target, llm_queue.py:347-398, plus the claim token,
rfp_create_files.py:546-580) serializes passes, so two `stage_late` calls for
one project never overlap and every re-check state change is serialized
(D3d); `bid_split_files_job_sandbox_uidx` makes a double stage impossible
anyway; `project_files` (project_id, rfp_sandbox_file_id) and (project_id,
bid_split_segment_id) unique indexes make a double promotion impossible
(rfp_create_files.py:28-32, 0132).

### 5d. Re-check (`rfp_file_rescue`, package C; `retry_run`, package B)

```python
class RescueRefused(Exception):
    def __init__(self, message: str, *, status: int = 409) -> None: ...

@dataclass(frozen=True)
class RecheckCandidate:
    sandbox_file_id: str
    run_id: str
    quarantine_path: str | None

@dataclass(frozen=True)
class RecheckOutcome:
    started: tuple[tuple[str, tuple[str, ...]], ...]   # (run_id, sandbox file ids reset)
    refused: tuple[tuple[str | None, str], ...]        # (run_id or None, app-authored sentence)
    transient: bool                                    # nothing started for a reason worth retrying
    gone: tuple[str, ...]                              # sandbox file ids whose stored copy is missing

def request_recheck(sb, project_id: str, *, actor_id: str,
                    settings: Settings | None = None) -> dict: ...
def run_recheck(sb, project_id: str, candidates: list[RecheckCandidate], *,
                actor_id: str | None, settings: Settings,
                renew: Callable[[], None]) -> RecheckOutcome: ...
```

`rfp_ingest.retry_run` gains a keyword and a busy subclass (package B):

```python
class RfpIngestBusy(RfpIngestPermanent): ...     # a job on the run, or the run moved
def retry_run(run_id: str, *, created_by: str | None, background: Any | None,
              file_ids: Collection[str] | None = None) -> dict: ...
```

- With `file_ids is None` it is today's function (the dev route,
  routers/rfp_ingest.py:474-487) plus the reset write below.
- With `file_ids`: the run gate is `_RUN_RETRYABLE | {RUN_DONE}` (a `done`
  run can hold gapped files), the reset set is `_FILE_RETRY_RESET +
  [STATUS_VERIFIED_WITH_GAPS]` (:199) restricted to `file_ids`, files
  outside `file_ids` are never touched, and when no listed file is in a
  resettable status it raises `RfpIngestPermanent("None of these files can
  be checked again.", http_status=409)` BEFORE the run CAS. Rejected files
  are still never revisited (protocol.py:360-362). The returned run dict
  gains `"reset_file_ids": [...]`.
- Both paths: the per-file reset update (:2459-2482) also writes
  `prior_fail_code = <the row's reject_code before the reset>` (the loop's
  select reads `id, status, reject_code`), which D1b reads.
- "A job is already ... for this run." (:2438-2443), "The run changed while
  retrying; reload it." (:2452) and the `JobAlreadyActive` put-back
  (:2487-2500) raise `RfpIngestBusy` (same sentences, same 409).

**The request (`request_recheck`, route `POST /rfp-created/{id}/recheck`).**
Refusals, first wins (sentences final):

1. `not settings.llm_queue_enabled` -> 503 "Re-checks need the document
   queue, which is turned off." `not settings.rfp_ingest_enabled` -> 503
   "The ingestion sandbox is turned off." (the queue skips sandbox claims
   while off, llm_queue.py:505-510).
2. Record missing -> the router 404s before calling.
3. `recheck_auto == 'scheduled'` -> 409 "An automatic re-check runs at
   {h:mm AM/PM Pacific}." (D3b.)
4. `recheck_auto in ('due', 'running')`, `recheck_requested_at` set, or
   `recheck_runs` non-empty -> 409 "A re-check is already running for this
   project."
5. `recheck_last_at` within `rfp_file_recheck_cooldown_seconds` -> 409 "A
   re-check was started moments ago; wait for it to finish."
6. `files_recheckable == 0`: when `files_rerunnable > 0` -> 409 "The stored
   copies of these files have expired; a new harvest is needed.", else 409
   "No document is waiting for a re-check."
7. CAS `recheck_requested_at = now, recheck_requested_by = actor_id,
   files_pass_requested_at = now where project_id = X and
   recheck_requested_at is null and recheck_runs = '{}' and (recheck_auto is
   null or recheck_auto = 'done')` (`.eq("recheck_runs", "{}")` is a literal
   string, 4.3); no row -> 409 "A re-check is already running for this
   project."
8. `enqueue_pass(sb, project_id, created_by=actor_id)`; audit
   `rfp_created.recheck_request` `{files: files_recheckable}`; returns
   `{"queued": True, "files": files_recheckable, "pass_queued": job is not
   None}`.

**The work (`run_recheck`, called by the pass, 5a step 7).**

1. Candidates without `quarantine_path` go to `gone`.
2. Byte check, at most 200 calls, `renew()` every 20:
   `rfp_ingest_storage.object_exists(QUARANTINE_BUCKET, path)` False ->
   `gone`; `RfpStorageError` -> stop, `transient = True`, refused (None,
   "Storage did not answer; the re-check will be tried again.") and return
   with nothing started.
3. Group the rest by `run_id` (a `reused` entry's file belongs to another
   harvest's run). For each run: `rfp_ingest.retry_run(run_id,
   created_by=actor_id, background=None, file_ids=ids)`. `RfpIngestBusy` ->
   refused with its sentence, `transient = True`. `RfpIngestPermanent` ->
   refused with its sentence. Success: when `llm_queue.active_job(
   JOB_RFP_INGEST, run_id)` is None afterwards (dispatch's enqueue failed
   with no BackgroundTasks, rfp_ingest.py:1016-1021):
   `rfp_ingest.cancel_if_unowned(run_id, grace_seconds=0)`, refused
   "The sandbox job could not be queued; the re-check will be tried
   again.", `transient = True`; else `started += (run_id,
   reset_file_ids)`.
4. `rfp_create_files.request_pass_for_runs(sb, [started run ids],
   exclude_project_id=project_id)`: every OTHER project holding those runs
   reclassifies the files as checking and follows them through (D2c).
5. `transient` is reported only when nothing started.

`rfp_ingest_storage.object_exists(bucket: str, path: str) -> bool` (package
B): a `GET` of `_object_url(bucket, path)` (rfp_ingest_storage.py:321-325)
with `_service_headers()` and `Range: bytes=0-0`, streamed and closed
without reading; 200 or 206 -> True; `_is_not_found` (:344-360) -> False;
anything else raises `RfpStorageError`. It never returns bytes to anyone.

The rest is 5c: the pass records the started runs in `files_wait.runs`,
the sweep sees them end and asks for a pass, the pass rescues the newly
verified files into the split job (or whole, or `additional` after the
hand-off) and records the result.

### 5e. Automatic retry (once, about 10 minutes later)

Mechanism, verified: `llm_jobs.next_attempt_at` gates claims
(0094_llm_job_queue.sql:57; claim RPC 0119_rfp_ingestion_sandbox.sql:254)
but `llm_queue.enqueue` takes no delay (llm_queue.py:347-398), and a delayed
`rfp_ingest` job would hold the run's one active-job slot for 10 minutes.
So the schedule is a record timestamp (`recheck_auto`, `recheck_auto_at`)
read by the existing queue sweep (5c step 1), not a delayed job.

1. A pass that leaves at least one rerunnable file a re-check can act on
   (`files_recheckable > 0`) schedules it (5a step 11), only from
   `recheck_auto is null`: once per project, ever.
2. At `recheck_auto_at` the sweep CASes `scheduled -> due` (clearing
   `recheck_auto_at`) and asks for a pass in the same write.
3. The pass (mode `auto`) runs `run_recheck`: runs started -> `running`;
   a transient refusal -> `scheduled` again 5 minutes later (at most 6
   tries); a final refusal -> `done`.
4. The started runs end; the sweep asks for a pass; the pass sees
   `recheck_runs` empty and sets `done`; when files are still rerunnable it
   bells the Estimating Admin once (5a step 12.1).
5. From then on the Re-check button is offered (`recheck_available`,
   section 6).

### 5f. IT Admin inspect and release (`rfp_file_rescue`, package C)

```python
def inspect(sb, project_id: str, sandbox_file_id: str, *, actor_id: str) -> dict: ...
def release(sb, project_id: str, sandbox_file_id: str, *, actor_id: str, note: str,
            confirm_unsafe: bool, reviewed_code: str, reviewed_keys: list[str],
            settings: Settings | None = None) -> dict: ...
```

Gate: `require_role(Role.IT_ADMIN)` on both routes (D2a).

`inspect` (read, audited `rfp_file.inspect`): `live = live_file(sb,
project_id, sid)` (404 "This file is not part of this project's
invitation." when None); loads `rfp_ingest.get_file` (rfp_ingest.py:
2585-2596) and the run; returns the facts of section 6 (including the
stored scan, or "not scanned yet") plus `rfp_ingest.file_urls(
sandbox_file_id)` (rfp_ingest.py:2646-2683: images PDF parts, text,
manifest; all from the DERIVED bucket). Never a quarantine URL (D2b).
`keys` in the payload is `live.verdict.keys`: the set the release form
sends back as `reviewed_keys`.

`release` steps, first refusal wins:

1. `settings.llm_queue_enabled` false -> 503 "Releases need the document
   queue, which is turned off."
2. `live = live_file(...)`; None -> 404 as inspect.
3. `live.verdict` None or `tier not in HELD_TIERS` -> 409 "This file is not
   held by the safety checks."
4. `file_row.released_at` set and `live.verdict.code != "released:changed"`
   -> 409 "This file was already released."
5. `code in NEVER_RELEASE` (or `may_release` False for a reason other than
   expiry) -> 409 "This file cannot be released: {message}"
6. Run `expired` -> 409 "The stored copy has expired; a new harvest is
   needed." No `quarantine_path` -> 409 "No copy of this file was kept; a
   new harvest is needed."
7. `tier == unsafe and not confirm_unsafe` -> 422 "Confirm that you
   inspected this file and accept the risk of adding it."
8. `reviewed_code != live.verdict.code` or `set(reviewed_keys) !=
   set(live.verdict.keys)` -> 409 "The findings for this file changed since
   you opened it; reload it and review it again."
9. `scan = rfp_create_files.scan_file(file_row, scratch, s)` (the scratch
   directory is removed afterwards): `Skip(changed_since_verification)` ->
   409 "The stored copy no longer matches the file that was checked; it
   cannot be released."; `Skip(missing_in_storage)` -> 409 "The stored copy
   is gone; a new harvest is needed."; `RfpCreateFilesTransient` -> 503
   "Storage did not answer; try again." `store_scan(sb, sid, scan)`. Then
   `full = live_keys({**file_row, "promotion_scan": scan.to_json(now)})`;
   `full != set(reviewed_keys)` -> `request_pass(sb, project_id)` and 409
   "The file carries findings you were not shown: {labels}. Reload it and
   review it again." (`{labels}` = the new keys joined by ", ").
10. CAS `update rfp_ingest_files set released_at = now, released_by =
    actor_id, released_project_id = project_id, released_tier, released_code,
    released_keys = sorted(full), released_status = file_row.status,
    released_note = note.strip(), released_sha256 = scan.sha256 where id =
    sid` fenced on the value read (`.is_("released_at", "null")` for a
    first release, `.eq("released_at", <read>)` for a re-release of a P12
    file); no row -> 409 "This file was already released."
11. `enqueue_pass(sb, project_id, created_by=actor_id)` and
    `request_pass_for_runs(sb, [file_row.run_id],
    exclude_project_id=project_id)` (never raise).
12. `audit(actor_id, "rfp_file.release", "rfp_ingest_file", sid, {...})`
    (4.5) inside a try/except that logs: the release columns are the
    durable record, so an audit failure never turns a done release into a
    500. Bench event `create.released`.
13. Returns `{"released": {...}, "pass_queued": bool}`.

The pass then: `promotion_for` admits the released bytes within the
reviewed set (5a); after the hand-off it lands as `additional` with
`handoff_note("released")`; before the hand-off, when `splitter_ok` and
`may_stage` and the split is on, it is staged into the harvest's split job
(every row it lands as carries the release note); otherwise it is promoted
whole with `release_note`.

### 5g. Landing after the estimator hand-off (package A)

Verified rules the promotion honors:

- `handoff_locked(project_id)` is true once any `file_send_batches` row, a
  sent assignment, or a sent update exists (files.py:187-217); it never
  turns false again, so the pass re-reads it only while it is false.
- `additional` is an UPDATE category (file_categories.py:130) that the
  upload route accepts only with a note and only after the lock
  (files.py:435-441, `NOTE_REQUIRED_MESSAGE` files.py:151). The note rule
  is app level only (0048_file_updates.sql:18-24, no DB CHECK), so the
  promotion must supply the note itself. `doc_type` must stay null
  (0077 CHECK allows it only on revision and addendum,
  0077_file_doc_type.sql:65-69); the addendum fields stay null (0132 CHECK).
- Unsent: `sent_to_estimators_at` stays null, so estimators cannot read it
  (`_estimator_visible`, files.py:235-240) and it appears in the Estimating
  Admin's pending updates (`_unsent_updates`, estimator.py:148-161), sent
  with the existing "Send updates" (`POST
  /projects/{project_id}/send-file-updates`, estimator.py:898-, require_writer),
  which stamps it (file_sends.py:310-330). The note stays editable (`PATCH
  .../files/{file_id}/note`, files.py:584-622).
- Initial categories are readable by estimators as soon as they exist
  (estimator.py:140-145), which is why `locked` is re-read before each
  landing decision rather than once per pass.

Promotion after the lock (5a step 5.8): whole file, `category =
"additional"`, `note = handoff_note(kind, ...)` by provenance (D4b),
`rfp_sandbox_file_id` and `rfp_harvest_id` set (so the unique index still
prevents a duplicate), no `promote_split_file` call for any file under the
lock, and one `rfp_create.files_added_after_handoff` bell per pass that
inserted at least one such row. `additional` is not a drawing category, so
no drawing bell.

### 5h. Retention exemption and expiry (package B; state written by A)

`rfp_ingest.prune_expired` (rfp_ingest.py:3039-3083) changes:

1. Before the loop: `held = {run ids}` from one read `select hold_run_ids
   from rfp_created_projects where hold_run_ids <> '{}'`
   (`.neq("hold_run_ids", "{}")`); `cap_cutoff = now -
   rfp_file_hold_retention_days`.
2. The page query selects `id, status, completed_at, created_at` and pages
   with `.range(offset, offset + _PRUNE_PAGE - 1)`, `offset` starting at 0.
3. For each row: when `id in held` and `created_at > cap_cutoff`: keep it
   (`kept_for_hold += 1`), continue. Otherwise the existing CAS to
   `expired`, path clearing and prefix deletes (:3072-3081); a lost CAS
   (a retry took the run back, :3072-3073) is neither kept nor expired.
   Expired runs that were in `held` are collected in `expired_held`.
4. Loop exit: `len(rows) < _PRUNE_PAGE`, or nothing expired and nothing
   kept; else `offset += kept_for_hold` (expired rows and rows a retry took
   back leave the filter; only rows deliberately kept stay in it).
5. After the loop: `rfp_create_files.request_pass_for_runs(sb,
   expired_held)`, so each project holding an expired run is reclassified
   (`may_recheck` / `may_release` off, the counts and `files_recheckable`
   refreshed).

`hold_run_ids` is rewritten by every promotion pass (5a step 11), so a
project whose files are all resolved stops holding its runs at the next
pass, and the next hourly prune expires them if they are past 14 days.
After expiry: `classify` turns `may_recheck` / `may_release` off (3.1), the
files view overlays the live run status (section 6), and the buttons show
"a new harvest is needed". `quarantine_path` stays set after expiry
(:2963-2983), which is why every action checks the run status and the
object itself (3.4).

### 5i. /rfp-processing stuck kind (packages C and D)

Backend (`app/services/rfp_processing.py`, package C):

```python
STUCK_SANDBOX_WAIT = "sandbox_wait"
STUCK_KINDS = (STUCK_FAILED, STUCK_RETRYING, STUCK_MODEL_WAIT, STUCK_STALLED, STUCK_SANDBOX_WAIT)

def classify_row(row, *, now, source, max_attempts, stall_minutes, slow_stall_minutes,
                 harvest_status, sandbox_wait_since: datetime | None = None,
                 sandbox_wait_minutes: int = 20) -> tuple[str, dict | None]: ...
def sandbox_wait_ids_to_check(rows: list[dict]) -> set[str]: ...    # rows at split with a harvest_id
def sandbox_waits(sb, ids) -> dict[str, str]: ...                   # harvest_id -> split_sandbox_wait_started_at
def classify_all(rows, *, source, now, settings, harvests,
                 sandbox_waits: dict[str, str] | None = None): ...
```

- New rule 4b in `classify_row` (rfp_processing.py:144-208), after the
  model-wait rule and before the harvest-active and stall rules: `status ==
  "split"` and `sandbox_wait_since` is set and `now - sandbox_wait_since >
  sandbox_wait_minutes` -> `(LANE_STUCK, _stuck(STUCK_SANDBOX_WAIT, "split",
  row, max_attempts))` with `since` = the wait stamp's ISO string. Email and
  portal rows alike (both reach `split`, PORTAL_PENDING :74).
- `sandbox_waits`: `select id, split_sandbox_wait_started_at from
  rfp_harvests where id in (...) and split_status = 'none' and
  split_sandbox_wait_started_at is not null`, chunked by 200.
- `classify_all` reads `settings.rfp_processing_sandbox_wait_minutes` and
  passes `sandbox_wait_since` per row.
- `summarize` counts the new kind automatically (it iterates
  `STUCK_KINDS`, :334-354).
- Router (`app/routers/rfp_processing.py` `_classified`, :121-145): compute
  `waits = svc.sandbox_waits(sb, svc.sandbox_wait_ids_to_check(emails) |
  svc.sandbox_wait_ids_to_check(portals))` and pass it to both
  `classify_all` calls. The Retry route is unchanged (a `sandbox_wait` row
  is not offered Retry on the FE).

Frontend lane maps (`bdr_fe/lib/rfpProcessing.ts`, package D):
`RfpStuckKind` and `RFP_STUCK_KINDS` (:153-155) gain `"sandbox_wait"`;
`isRfpProcessingRetryable` (:260-264) returns false for it (unchanged
logic already does); `stuckSentence` in
`app/(app)/rfp-processing/page.tsx` (:276-293) gains the case with
`rfpProcessing.stuckReason.sandbox_wait` and `fmtDateTime(s.since)`.

### 5j. Test bench events (`rfp_test.record`, rfp_test.py:478-508)

Session id: `harvest.test_session_id` (split events) or
`rfp_created_projects.test_session_id` (0131_rfp_testing.sql:111; create
events). `record` is a no-op without one. Existing sources only
(`SOURCE_SPLIT`, `SOURCE_CREATE`, rfp_test.py:65-72; the column has no
CHECK, 0131_rfp_testing.sql:60). Events are recorded for frozen sessions
too (harmless; they explain why nothing else happened).

| Source | Kind | Level | Title | Detail | Owner |
|---|---|---|---|---|---|
| split | `sandbox_deadline` | warn | Split stopped waiting: the sandbox did not finish in {n} minutes | `{harvest_id, sandbox_run_id, still_checking, total, canceled}` (once per harvest, 5b step 4) | B |
| split | `late_staged` | info | Split: {n} held-back document(s) staged late | `{harvest_id, job_id, created_job, staged, refused}` | B |
| create | `verdicts` | info (warn when any held tier) | Documents: {p} added, {u} unsafe, {h} held, {r} re-check, {c} checking, {x} not usable | `{counts, files: first 50 SkippedEntry}` | A |
| create | `alerted` | warn | IT Admin alerted about {n} held file(s) (or: no IT Admin to alert) | `{files: [{key, name, tier, code}], recipients}` | A |
| create | `added_after_handoff` | info | {n} file(s) added under Additional files after the hand-off | `{project_file_ids, kinds}` | A |
| create | `recheck` | info | Re-check started ({auto or manual}): {f} file(s) in {r} run(s) | `{auto, runs, gone, refused}` | A |
| create | `recheck_refused` | warn | Re-check could not start ({auto or manual}): {sentence} | `{auto, transient, refused, gone}` | A |
| create | `followup` | info | Follow-up: {reason} | `{project_id, reason: auto_recheck or files_wait or pass_requested}` | C |
| create | `released` | warn | IT released {name} | `{sandbox_file_id, tier, code, keys, by}` | C |

Bench API (`app/routers/rfp_testing.py`, package C): `_project_flags`
(:534-548) adds `files_unsafe`, `files_held`, `files_rerunnable`,
`files_checking` (each when > 0) and `rechecking`; the project detail
`rfp_created` block (:787-795) adds `files_counts` (the five columns).
`files_skipped` keeps passing the list through; the FE type is fixed in
package D (lib/rfpTesting.ts:361 declares a number today, tabs.tsx:520
interpolates it).

---

## 6. API

All new routes live in `app/routers/rfp_created.py` (router-level gate
`require_rfp_created`, 404 while neither the email intake nor NGEM is
served, rfp_created.py:57-85). Handlers are plain `def` (the Supabase SDK
is sync). Literal paths before parameterized ones. Every path id is
validated with `_uuid_or_404` (the file's own pattern, rfp_created.py:502,
523, 554): `project_id` with `_NOT_FOUND`, `sandbox_file_id` with "This
file is not part of this project's invitation."

| Method | Path | Gate | Rate limit | Status codes |
|---|---|---|---|---|
| GET | `/rfp-created/{project_id}/files` | `require_internal` (deps.py:253-263) | `rfp_created_rate_limit` (:77-79) | 200, 404 |
| POST | `/rfp-created/{project_id}/recheck` | `require_page` (PAGE_ROLES, :74-75) | `ai_rate_limit` (ratelimit.py:110-111; the dev run retry uses it, routers/rfp_ingest.py:474) | 202, 404, 409, 503 |
| GET | `/rfp-created/{project_id}/files/{sandbox_file_id}/inspect` | `require_it_admin = require_role(Role.IT_ADMIN)` | `rfp_created_rate_limit` | 200, 404 |
| POST | `/rfp-created/{project_id}/files/{sandbox_file_id}/release` | `require_it_admin` | `ai_rate_limit` (it downloads and hashes up to the per-file cap) | 202, 404, 409, 422, 503 |
| POST | `/rfp-created/{project_id}/retry-files` | unchanged | unchanged | unchanged, plus the narrowed 409 below |

The module docstring (:11-20) is updated: the files list is readable by
every internal role (the project page shows it to engineers too); release
and inspect are IT Admin only; re-check and release use the AI budget;
everything else stays PAGE_ROLES and the default budget.

**GET files** (`rfp_file_rescue.files_view(sb, project_id, *, viewer_role:
Role, settings: Settings | None = None) -> dict`). 404 "Created project
record not found" (`_NOT_FOUND`). Serves the stored `files_skipped` (listed
entries only; legacy entries through `legacy_tier`), overlays the live run
status (one `run_statuses` read: an `expired` run turns `may_recheck` and
`may_release` off and appends the expiry sentence) and the live release
(`released_at`, `released_by` name, `released_note` from one
`rfp_ingest_files` read).

```json
{
  "project_id": "uuid",
  "counts": {"unsafe": 1, "held": 2, "rerunnable": 0, "checking": 0, "not_usable": 3},
  "files": [
    {"key": "uuid", "sandbox_file_id": "uuid", "harvest_id": "uuid",
     "file_path": "name.pdf", "tier": "held", "code": "marker:/AA",
     "message": "The file has automatic actions (common in CAD and Bluebeam exports).",
     "keys": ["/AA"], "may_release": true, "may_recheck": false,
     "released": null}
  ],
  "recheck": {"available": false, "reason": "An automatic re-check runs at 3:42 PM.",
              "running": false, "requested": false, "auto_state": "scheduled",
              "auto_at": "iso", "last_at": null, "last_by_name": null,
              "last_auto": false, "last_refused": []},
  "it": {"unalerted": 0},
  "viewer": {"can_recheck": true, "can_release": false}
}
```

`released`: `{"at": iso, "by_name": str | null, "note": str}`.
`recheck` is `rfp_file_rescue.recheck_state(record, *, now, settings) ->
dict`: `running` = `recheck_runs` non-empty or `recheck_auto in ('due',
'running')`; `requested` = `recheck_requested_at` set; `auto_at` =
`recheck_auto_at` only while `recheck_auto == 'scheduled'`, else null;
`available` = none of the request refusals 1 and 3 to 6 of 5d applies
(computed from the record alone, no storage call); `reason` = that
refusal's sentence, else null; `last_refused` = the sentences in
`recheck_last_result.refused`. `viewer.can_recheck` = role in PAGE_ROLES;
`viewer.can_release` = role is `it_admin`.

**POST recheck** (`rfp_file_rescue.request_recheck(..., actor_id=user.id)`).
202:

```json
{"queued": true, "files": 2, "pass_queued": true}
```

409 / 503 bodies are `{"detail": "<sentence of 5d>"}`. Audited by the
service (`rfp_created.recheck_request`).

**GET inspect** (audited `rfp_file.inspect`). 404 "This file is not part of
this project's invitation."

```json
{"sandbox_file_id": "uuid", "file_path": "name.pdf", "tier": "unsafe",
 "code": "hazard:page_actions", "message": "The file contains JavaScript.",
 "keys": ["/JS", "page_actions"], "status": "verified", "reject_code": null,
 "source_format": "pdf", "size_bytes": 123456, "sha256": "hex",
 "page_count": 12, "hazards": {"page_actions": 1, "uri_links": 3},
 "byte_markers": {"/JS": 1, "/OpenAction": 1},
 "scan": {"scanned": true, "pdf": "original", "marker_keys": ["/JS"], "ooxml": null, "at": "iso"},
 "sniff_magics": [], "run": {"id": "uuid", "status": "done", "expired": false},
 "bytes_available": true, "splitter_ok": true,
 "images_pdf": ["signed derived url", null], "text_url": "signed derived url",
 "released": null}
```

`byte_markers` is `manifest.sniff.byte_markers` (raw substring counts, for
the reviewer, rfp_ingest.py:831-837); `scan` is the valid `promotion_scan`
(`{"scanned": false}` when there is none: the release scans before it
commits and stops on anything new); `bytes_available` = run not expired
and `quarantine_path` set; `splitter_ok` tells IT whether the file would be
cut or added whole.

**POST release**. Body model in `app/models/schemas.py` (package C):

```python
class RfpFileReleaseIn(BaseModel):
    note: str                        # stripped; 1..1000 chars; control characters removed
    confirm_unsafe: bool = False
    reviewed_code: str               # the inspect payload's code
    reviewed_keys: list[str]         # the inspect payload's keys (max 100)
```

202:

```json
{"released": {"sandbox_file_id": "uuid", "at": "iso", "by": "uuid",
              "tier": "held", "code": "marker:/AA", "keys": ["/AA"], "sha256": "hex"},
 "pass_queued": true}
```

404 as inspect; 409 / 422 / 503 sentences exactly as 5f; 422 for a bad
note ("A note of up to 1000 characters is required.").

**POST retry-files** (narrowed, DF1). `retryable` no longer admits
`complete` (5a); the 409 for a complete record becomes "The documents were
filed. Use Re-check files for documents the sandbox could not check."
(rfp_created.py:559-563). Everything else unchanged.

**GET /rfp-created list** (`_item` flags, rfp_created.py:351-376) adds:

```json
"files_unsafe": 0, "files_held": 0, "files_not_usable": 0,
"files_rerunnable": 0, "files_checking": 0, "files_recheckable": 0, "files_unalerted": 0,
"rechecking": false, "recheck_auto": null, "recheck_auto_at": null, "recheck_available": false
```

`documents_skipped` stays (listed entries count; legacy clients).
`recheck_available` and `recheck_auto_at` follow `recheck_state` (record
only, no run read).

**ProjectOut.rfp_created** (`RfpCreatedSummary`, schemas.py:521-532;
`_RFP_CREATED_SELECT` projects.py:241-245; `_rfp_created_summary`
projects.py:277-287) adds:

```python
files_unsafe: int = 0
files_held: int = 0
files_not_usable: int = 0
files_rerunnable: int = 0
files_checking: int = 0
files_recheckable: int = 0
files_unalerted: int = 0
rechecking: bool = False
recheck_auto: str | None = None
recheck_auto_at: datetime | None = None      # only while recheck_auto == "scheduled"
recheck_available: bool = False             # rfp_file_rescue.recheck_state(...)["available"]
```

(The select adds `files_unsafe, files_held, files_not_usable,
files_rerunnable, files_checking, files_recheckable, files_unalerted,
recheck_runs, recheck_auto, recheck_auto_at, recheck_requested_at,
recheck_last_at`.) Served on the list and the detail route (projects.py:
364-402, 787-804); internal roles only (projects.py:789-790).

**RFP email detail and NGEM invitation detail** (the harvest card, DF4):
`harvest.files[]` entries gain `verdict: {"tier", "code", "message",
"provisional"} | null`, filled by `rfp_create_files.annotate_entries`
inside `rfp_harvest.harvest_for_email` (rfp_harvest.py:2163-2184) and
`rfp_portal_ingest.harvest_for_invitation` (rfp_portal_ingest.py:2932),
package E, best effort.

---

## 7. Notifications

Three new types, all `rfp_create.*` so the mirror email never linkifies the
attacker-controlled filenames (`_NO_LINKIFY_PREFIX`,
notification_email.py:117-125). Package A adds to `_TYPE_META`
(notification_email.py:44-99):

```python
"rfp_create.files_held": ("RFP files held for IT review", "Open project"),
"rfp_create.files_recheck_needed": ("RFP files need a re-check", "Open project"),
"rfp_create.files_added_after_handoff": ("RFP files added after the hand-off", "Open project"),
```

`notify_role` (notifications.py:35-89, package A) now returns `int`: the
number of notification rows written (0 for the estimator refusal at
:62-68 and for no active user at :75-76). Existing callers ignore it.

| Type | Recipients | When | Dedupe |
|---|---|---|---|
| `rfp_create.files_held` | every active `it_admin` via `notify_role(Role.IT_ADMIN, ...)` (`profiles.role = 'it_admin' and is_active`) | a pass finds held-tier entries whose key is not in `it_alerted` or whose `tier:code` signature changed (an escalation, a `released:changed`) | `it_alerted`, recorded under the claim only after at least one bell was written (5a step 10); zero recipients leave the entries unalerted and counted in `files_unalerted` |
| `rfp_create.files_recheck_needed` | every active `estimating_admin` via `notify_role(Role.ESTIMATING_ADMIN, ...)` | the pass that ends the one automatic retry still leaves rerunnable files | once: `recheck_auto` reaches `done` once per project |
| `rfp_create.files_added_after_handoff` | every active `estimating_admin` | a pass inserted at least one `additional` row | per insert (the unique index admits each file once) |

`mirror_email=True` for all three; none is sent for a record whose bench
session has ended (`session_frozen`, DF5). `label = f"Project {number}
{name}"` (stripped). Messages (`rfp_file_verdicts`; every name through
`clean_name(file_path, 80)` and wrapped in straight double quotes):

- `held_alert_message(label, entries)`: "{label}: {n} file(s) from the RFP
  invitation were held back by the file safety checks ({u} may be
  compromised, {h} held by the file safety rules): {names}. Review them on
  the project page. Nobody should open these files from the email or the
  bid portal." `{names}`: up to 10 quoted names joined by "; ", then "; and
  {k} more". The counts phrase omits a zero part. "file" / "files" by n.
- `recheck_needed_message(label, n)`: "{label}: {n} file(s) from the RFP
  invitation still could not be checked after the automatic retry. Open the
  project to see why and re-check them."
- `added_after_handoff_message(label, counts)`: "{label}: {n} file(s) from
  the RFP invitation were added under Additional files after the estimator
  package was sent ({r} held back at creation and now cleared, {x} released
  by IT, {a} from a later invitation). They have not been sent to the
  estimators; review the notes and use Send updates." Zero parts omitted.

Metadata (notifications.metadata jsonb, 0118):

- `files_held`: `{"project_id", "number", "counts": {tier: n},
  "files": [{"sandbox_file_id", "name", "tier", "code"}]}` (files capped at
  20).
- `files_recheck_needed`: `{"project_id", "number", "rerunnable": n}`.
- `files_added_after_handoff`: `{"project_id", "number", "count": n,
  "kinds": {"rescued": n, "released": n, "added": n}, "project_file_ids":
  [...]}` (capped at 20).

Deep links: the bell falls through to `/projects/{project_id}`
(NotificationsBell.tsx:174-192, no FE change, B2); the mirror email button
goes to the same page (`_deep_link`, notification_email.py:162+). The
project page Callout (section 8) is at the top.

Dismissals: 5a step 12.3 (`dismiss_notifications`, notifications.py:113-170).

Test bench redirect, verified: while a test session is active EVERY
`graph_email.send_mail` is rewritten to the session's redirect address
(graph_email.py:13-18, 254-262, 536-545), and the notification mirror sends
through `send_mail` (`_send_one`, notification_email.py:296-318). The
redirect keys on the ACTIVE session, not on the record, so this slice never
rings for a record whose session has ended (DF5); with no bench session
the mirrors go to the real dev profiles (IT Admin, Estimating Admin), which
is why the live test (section 10) runs inside a session.

---

## 8. Frontend

Design system: squared-off neumorphic navy-on-light, no glass (the UI kit in
`bdr_fe/components/ui`, index.ts). Reuse `Badge` (tones neutral, ok, warn,
danger, info, navy), `Callout` (info, warn, neutral, danger, success;
Callout.tsx:5-11), `Button` (primary, secondary, success, danger, ghost,
link; Button.tsx:4), `ButtonLink`, `buttonClasses`, `Modal` (the sanctioned
elevation exception, Modal.tsx), `Textarea`, `Checkbox` (`checked`,
`onChange`), `Field`, `Spinner`, `ErrorText`, `DateTime`. No new
primitives. Tier tones: unsafe `danger`, held `warn`, rerunnable `info`,
checking `info` (with `Spinner`), not_usable `neutral`. Every string
through `t()`; no em dashes. Filenames render as plain text (React
escapes), `font-mono text-xs break-all`, as RfpHarvestBlock.tsx:968-971
does.

### 8.1 New files

**`bdr_fe/lib/rfpFileVerdicts.ts`** (leaf module over lib/api, lib/types,
lib/rfpCreated types):

```ts
export type RfpFileTier = "unsafe" | "held" | "not_usable" | "rerunnable" | "checking";
export const RFP_FILE_TIERS: readonly RfpFileTier[] = ["unsafe", "held", "rerunnable", "checking", "not_usable"];
export const RFP_FILE_TIER_TONE: Record<RfpFileTier, "danger" | "warn" | "info" | "neutral">;
export type RfpFileTierCounts = Record<RfpFileTier, number>;
export interface RfpFileRelease { at: string; by_name: string | null; note: string }
export interface RfpFileVerdict {
  key: string; sandbox_file_id: string | null; harvest_id: string | null; file_path: string;
  tier: RfpFileTier; code: string; message: string; keys: string[];
  may_release: boolean; may_recheck: boolean; released: RfpFileRelease | null;
}
export interface RfpRecheckState {
  available: boolean; reason: string | null; running: boolean; requested: boolean;
  auto_state: "scheduled" | "due" | "running" | "done" | null; auto_at: string | null;
  last_at: string | null; last_by_name: string | null; last_auto: boolean; last_refused: string[];
}
export interface RfpFileVerdictsResponse {
  project_id: string; counts: RfpFileTierCounts; files: RfpFileVerdict[];
  recheck: RfpRecheckState; it: { unalerted: number };
  viewer: { can_recheck: boolean; can_release: boolean };
}
export interface RfpFileInspect { /* section 6 inspect shape, including scan */ }
export interface RfpRecheckResult { queued: boolean; files: number; pass_queued: boolean }
export interface RfpReleaseResult { released: { sandbox_file_id: string; at: string; by: string; tier: RfpFileTier; code: string; keys: string[]; sha256: string }; pass_queued: boolean }
export interface RfpHarvestVerdict { tier: RfpFileTier; code: string; message: string; provisional: boolean }

export function fetchRfpFileVerdicts(projectId: string): Promise<RfpFileVerdictsResponse>;        // GET /rfp-created/{id}/files
export function recheckRfpFiles(projectId: string): Promise<RfpRecheckResult>;                    // POST .../recheck
export function inspectRfpFile(projectId: string, sandboxFileId: string): Promise<RfpFileInspect>; // GET .../files/{sid}/inspect
export function releaseRfpFile(projectId: string, sandboxFileId: string,
  body: { note: string; confirm_unsafe: boolean; reviewed_code: string; reviewed_keys: string[] }): Promise<RfpReleaseResult>; // POST .../files/{sid}/release
export const RFP_FILE_RECHECK_ROLES: readonly Role[];   // = RFP_CREATED_ROLES
export function canRecheckRfpFiles(role: Role | null | undefined): boolean;
export const RFP_FILE_RELEASE_ROLES: readonly Role[];   // ["it_admin"]
export function canReleaseRfpFiles(role: Role | null | undefined): boolean;
export type RfpFileCaution = "unsafe" | "held" | null;
export function rfpFileCaution(summary: RfpCreatedSummary | null | undefined): RfpFileCaution; // unsafe > 0 -> "unsafe"; held > 0 -> "held"; else null
export function rfpFileCounts(src: Partial<Record<`files_${RfpFileTier}`, number>> | null | undefined): RfpFileTierCounts;
export function rfpFileKeyLabel(key: string, t: TFunction): string; // rfpFiles.keys.<key with "/" removed; ":" replaced by "_">, defaultValue key
```

**`bdr_fe/components/RfpFileVerdictsModal.tsx`**:
`RfpFileVerdictsModal({ projectId, open, onClose, onChanged })`
(`onChanged?: () => void`, called after a re-check request or a release so
the parent reloads the project or the row). `Modal` size `lg`, title
`rfpFiles.title`. Loads `fetchRfpFileVerdicts` on open. Top: one Callout per
non-empty tier among unsafe, held, rerunnable, checking with
`rfpFiles.tierHelp.<tier>`; when `it.unalerted > 0` a danger line
`rfpFiles.noItAdmin`; for `viewer.can_release` the line
`rfpFiles.itAdminHint`. A re-check bar when `counts.rerunnable > 0`: the
`rfpFiles.recheck.button` Button when `viewer.can_recheck &&
recheck.available`, else the state text (`rfpFiles.recheck.running` when
`running` or `requested`, `rfpFiles.recheck.autoAt` with the formatted
`auto_at` when set, or `recheck.reason`), plus `rfpFiles.recheck.lastBy` /
`lastAuto` when `last_at` and one `rfpFiles.recheck.lastRefused` line per
`last_refused` sentence. After a successful request: `rfpFiles.recheck.queued`
and `onChanged()`. Body: files grouped by tier in `RFP_FILE_TIERS` order,
each row: name (mono), tier `Badge`, the backend `message` (B3),
`rfpFiles.found` with the key labels when `keys` non-empty,
`rfpFiles.release.released` when `released`, and for `viewer.can_release
&& file.may_release && (!file.released || file.code ===
"released:changed")` a secondary Button `rfpFiles.release.button` opening
the release modal. Empty: `rfpFiles.empty`. Errors: `ErrorText` with the
API message.

**`bdr_fe/components/RfpFileReleaseModal.tsx`**:
`RfpFileReleaseModal({ projectId, file, open, onClose, onReleased })`
(`file: RfpFileVerdict`). Loads `inspectRfpFile`. Shows
`rfpFiles.release.intro`, the facts table (`rfpFiles.release.facts.*`),
the found keys (from the INSPECT payload's `keys`), the scan
(`facts.scan`: the marker labels, or `rfpFiles.release.notScanned`;
`facts.ooxml`: the reason or `notScanned`), `rfpFiles.release.wholeOnly`
when `splitter_ok` is false, `ButtonLink`s to each `images_pdf` part
(`rfpFiles.release.viewImages`, `imagesPart`; opens a new tab) or
`rfpFiles.release.noImages`; `bytes_available === false` shows
`rfpFiles.release.bytesGone` and disables submit. `Field` + `Textarea` for
the note (required, 1000 max, `rfpFiles.release.noteLabel`,
`notePlaceholder`, `noteRequired`). For `tier === "unsafe"`: a `Checkbox`
with `rfpFiles.release.confirmUnsafe` that must be checked. Submit sends
`reviewed_code` and `reviewed_keys` from the inspect payload; `Button`
variant `danger` for unsafe, `primary` for held, label
`rfpFiles.release.submit`; `Modal suppressClose` while submitting; on
success `rfpFiles.release.done` then `onReleased()`; a 409 shows the
backend sentence and reloads the inspect payload.

**`bdr_fe/components/RfpFileWarning.tsx`** exports:

- `RfpFileBadges({ summary }: { summary: RfpCreatedSummary | null | undefined })`:
  header badges, in this order: unsafe > 0 `Badge tone="danger"`
  `projectPage.rfpFiles.badge.unsafe`; held > 0 `tone="warn"`
  `badge.held`; rerunnable > 0 `tone="info"` `badge.rerunnable` (count);
  checking > 0 or `rechecking` `tone="info"` with `Spinner`
  `badge.checking`. Nothing when all are zero.
- `RfpFileCallout({ projectId, summary, onChanged }: { projectId: string;
  summary: RfpCreatedSummary | null | undefined; onChanged?: () => void })`:
  the project Callouts (rendered for every internal viewer): unsafe ->
  `Callout tone="danger"` `projectPage.rfpFiles.callout.unsafe` (count);
  held (when no unsafe, or in addition) -> `tone="warn"` `callout.held`;
  under either, `callout.noItAdmin` when `files_unalerted > 0` and
  `callout.itAdminHint` for `canReleaseRfpFiles(profile.role)`;
  rerunnable -> `tone="info"` `callout.rerunnable` plus, for
  `canRecheckRfpFiles(profile.role)`, the Re-check Button enabled only
  when `summary.recheck_available`, otherwise disabled with
  `rfpFiles.recheck.autoAt` when `recheck_auto === "scheduled"` and
  `recheck_auto_at` is set, or `rfpFiles.recheck.running` while
  `rechecking`; checking -> `tone="info"` `callout.checking`. Each Callout
  carries a ghost Button `projectPage.rfpFiles.callout.viewFiles` that
  opens `RfpFileVerdictsModal`. Calls `onChanged` after actions.
- `RfpCautionLink({ href, className, children, caution, loadFiles })`:
  `caution: RfpFileCaution`; `loadFiles: () => Promise<{ name: string;
  tier: RfpFileTier }[]>`. With `caution` null a plain `<a href
  target="_blank" rel="noopener noreferrer">`; otherwise a `Button` with
  the same classes that opens a `Modal` size `sm`: title
  `biddingLink.caution.title`, body `biddingLink.caution.unsafe` or
  `.held`, then `biddingLink.caution.listTitle` and up to 20 names (mono)
  with their tier Badge, then `biddingLink.caution.andMore` when more
  (a `loadFiles` failure shows `biddingLink.caution.loadError`), footer: a
  real `<a href={href} target="_blank" rel="noopener noreferrer">` styled
  `buttonClasses("secondary")` labelled `biddingLink.caution.open`
  (clicking it closes the modal) and a `common.cancel` Button. The link is
  never hidden (DF2).

### 8.2 Changed files

| File | Change |
|---|---|
| `bdr_fe/lib/rfpCreated.ts` | `RfpCreatedSummary` (:38-46) adds `files_unsafe?`, `files_held?`, `files_not_usable?`, `files_rerunnable?`, `files_checking?`, `files_recheckable?`, `files_unalerted?` (numbers), `rechecking?: boolean`, `recheck_auto?: string \| null`, `recheck_auto_at?: string \| null`, `recheck_available?: boolean`. `RfpCreatedFlags` (:144-160) adds the same. `canRetryRfpCreatedFiles` (:264-273) returns true only for `failed` / `none` with a harvest (the complete-with-skips branch goes). |
| `bdr_fe/app/(app)/projects/[id]/page.tsx` | Header (:772-795): `<RfpFileBadges summary={project.rfp_created} />` after the unauthorized-sender badge. Below the intake Callout (:843-849): `{!abandoned && <RfpFileCallout projectId={project.id} summary={project.rfp_created} onChanged={load} />}`. `GoNoGoPanel` (:1085) and `PricingPanel` (:1192) get `fileCaution={rfpFileCaution(project.rfp_created)}`. `FilesPanel` (:1415) gets `rfpCreated={project.rfp_created}`. |
| `bdr_fe/components/FilesPanel.tsx` | Props `{ projectId: string; rfpCreated?: RfpCreatedSummary \| null }` (:96). For internal viewers (`profile.role !== "estimator"`), when the sum of the five counts is > 0: a Callout above the file list (after the splitter line, :248-258) with `filesPanel.rfpFiles.notice` (count = the sum), tone danger / warn / info / neutral by the worst non-zero tier, and a ghost Button `filesPanel.rfpFiles.viewFiles` opening `RfpFileVerdictsModal`. |
| `bdr_fe/components/BiddingLinkButton.tsx` | New optional prop `caution?: RfpFileCaution` (default null). When `url` is set, the Open control (:76-90) renders through `RfpCautionLink` with the same classes, icon and label, `caution`, and `loadFiles` = `fetchRfpFileVerdicts(projectId)` filtered to `unsafe` / `held` entries (`name = file_path`). |
| `bdr_fe/components/GoNoGoPanel.tsx` | New optional prop `fileCaution?: RfpFileCaution`, forwarded as `caution` to `BiddingLinkButton` (:133-138). |
| `bdr_fe/components/PricingPanel.tsx` | Same prop, forwarded (:511-515). |
| `bdr_fe/components/GoNoGoModal.tsx` | `GoNoGoModalProject` (:14-22) adds `rfp_created?: RfpCreatedSummary \| null` (the Go/No-Go list reads `/projects?stage=go_no_go`, which carries it, projects.py:364-402); passes `fileCaution={rfpFileCaution(project.rfp_created)}` to `GoNoGoPanel` (:80-90). |
| `bdr_fe/app/(app)/rfp-created/page.tsx` | `FlagBadges` (:116-230): replace the `documents_skipped` neutral badge with tier badges `rfpCreated.flags.filesUnsafe` (danger), `filesHeld` (warn), `filesRerunnable` (info), `filesChecking` (info + Spinner), `filesNotUsable` (neutral), and `rechecking` (info + Spinner); when the five counts are absent (older backend) keep the old `documentsSkipped` badge. Actions (:673-700): a ghost `rfpCreated.actions.files` Button when any count > 0 (opens `RfpFileVerdictsModal`, `onChanged` reloads the page); a secondary `rfpCreated.actions.recheck` Button when `flags.recheck_available` (calls `recheckRfpFiles` through the page's `act`); "Retry documents" follows the narrowed `canRetryRfpCreatedFiles`. Buttons stop row-click propagation like the existing ones. |
| `bdr_fe/lib/rfpEmails.ts` | `RfpHarvestFile` (:470-486) adds `verdict?: RfpHarvestVerdict \| null`. |
| `bdr_fe/lib/rfpPortal.ts` | `RfpPortalHarvestFile` (:217-225) adds the same `verdict?`. |
| `bdr_fe/components/RfpHarvestBlock.tsx` | `FileList` (:917-994): when `f.verdict` is set, the row's status Badge is replaced by a Badge with `RFP_FILE_TIER_TONE[verdict.tier]`, label `rfpFiles.tier.<tier>`, and the verdict `message` is shown inline under the name (`text-xs text-ink-muted`), followed by `rfpFiles.provisional` when `provisional`; the header counts add one Badge per held tier present. Above the list, a Callout `rfpEmails.harvest.fileWarning.unsafe` (danger) or `.held` (warn) when any entry is unsafe / held. The link rows' "Open link" anchors (:880-889) render through `RfpCautionLink` with the card's caution (worst of the entries' held tiers) and `loadFiles` resolving the card's own unsafe / held entries (no fetch). The `FileRow` mapping carries `verdict` from both email and portal shapes. |
| `bdr_fe/lib/rfpProcessing.ts` | 5i. |
| `bdr_fe/app/(app)/rfp-processing/page.tsx` | 5i (`stuckSentence` case). |
| `bdr_fe/lib/rfpTesting.ts` | `rfp_created.files_skipped` (:361) becomes `RfpFileVerdict[] \| { file_path: string; reason: string }[] \| number \| null`; adds `files_counts?: RfpFileTierCounts \| null`. |
| `bdr_fe/app/(app)/rfp-testing/tabs.tsx` | :520 shows `Array.isArray(x) ? x.length : (x ?? 0)` and, when `files_counts` is present, the five counts. |

Role gating: the Callout, badges and modal list render for every internal
role; the Re-check button needs `canRecheckRfpFiles` (the backend's
`viewer.can_recheck` wins inside the modal) and `recheck_available`;
release needs `canReleaseRfpFiles` (`viewer.can_release` wins). The
estimator portal never renders any of it (FilesPanel gates on role; the
project page is internal only). The live test checks an engineer sees the
warnings and neither button (10.3 step 6).

### 8.3 i18n keys

Convention, verified: one `translation` namespace per locale, nested keys,
`fallbackLng` English (lib/i18n.ts:26-45). Neighbors: `rfpCreated.*`,
`projectPage.rfpCreated.*` and `rfpEmails.harvest.*` are translated in all
six catalogs (checked in en, hi, ur 2026-09-24); `rfpProcessing.*` is
English in all six (checked in hi, ur, sw); `biddingLink.*` exists only in
en today. Rule for this slice: every new key goes into all six catalogs
(`en hi ceb fil sw ur`); user-facing safety copy (`rfpFiles`,
`projectPage.rfpFiles`, `filesPanel.rfpFiles`, `biddingLink.caution`,
`rfpCreated` and `rfpEmails` additions) is translated; the ops keys
(`rfpProcessing` additions) are copied in English, matching their
neighbors. Placeholders `{{count}}`, `{{time}}`, `{{name}}`, `{{when}}`,
`{{since}}`, `{{list}}`, `{{reason}}` stay verbatim; `ur` is RTL (no layout
keys needed). Plural keys use `_one` / `_other` (the catalogs' existing
convention, e.g. `rfpCreated.flags.documentsSkipped_one`). Catalog files
must round-trip byte-identical through `json.dumps(obj,
ensure_ascii=False, indent=2) + "\n"` (verified for all six today). Do NOT
run `scripts/translate_catalog.py` (it regenerates whole catalogs); insert
only the new keys.

English source (`bdr_fe/locales/en/translation.json`):

```json
"rfpFiles": {
  "title": "Files from the RFP invitation",
  "empty": "Every file from the RFP invitation is in the project.",
  "loadError": "Could not load the file list.",
  "found": "Found: {{list}}",
  "provisional": "Early result; confirmed when the project is created.",
  "noItAdmin": "No IT Admin could be notified automatically. Tell an Executive so IT can review these files.",
  "itAdminHint": "As an IT Admin you can inspect each held file below and release it if it is safe.",
  "tier": {
    "unsafe": "May be compromised",
    "held": "Held for IT review",
    "rerunnable": "Needs a re-check",
    "checking": "Still checking",
    "not_usable": "Not usable"
  },
  "tierHelp": {
    "unsafe": "These files may be compromised. IT has been notified. Contact your IT Admin, and do not open these files from the email or the bid portal.",
    "held": "These files are held by the file safety rules. IT has been notified and can release them after checking them. Do not open them from the email or the bid portal.",
    "rerunnable": "The safety check did not finish for these files. Re-check files runs it again.",
    "checking": "These files are still being checked, or cut by the Bid File Splitter. They are added to the project when that finishes.",
    "not_usable": "These files could not be used, for the reason shown on each one. They were not flagged as unsafe; ask the sender for a copy that can be checked (for example one without a password)."
  },
  "keys": {
    "javascript_actions": "JavaScript",
    "launch_actions": "launch actions",
    "attachments": "embedded files",
    "file_attachments": "file attachments",
    "xfa_packets": "XFA form",
    "remote_goto": "links to other files",
    "embedded_goto": "links into embedded files",
    "page_actions": "page actions",
    "hazards_unknown": "unknown active content",
    "OpenAction": "open action",
    "AA": "automatic actions",
    "JavaScript": "JavaScript",
    "JS": "JavaScript",
    "Launch": "launch actions",
    "EmbeddedFile": "embedded files",
    "XFA": "XFA form",
    "RichMedia": "embedded media",
    "GoToR": "links to other files"
  },
  "recheck": {
    "button": "Re-check files",
    "running": "Re-check running",
    "queued": "Re-check queued. The list updates when the sandbox finishes.",
    "autoAt": "An automatic re-check runs at {{time}}.",
    "lastBy": "Last re-check {{when}} by {{name}}",
    "lastAuto": "Last re-check {{when}} (automatic)",
    "lastRefused": "The last re-check could not start: {{reason}}"
  },
  "release": {
    "button": "Inspect and release",
    "released": "Released by {{name}} on {{when}}",
    "title": "Inspect and release a held file",
    "intro": "Releasing adds this file to the project. Before the estimator hand-off it goes to the estimators with the package and can be attached to vendor RFQs under G3's name; after the hand-off it lands under Additional files. Check the findings and the page images first.",
    "facts": {
      "status": "Sandbox result",
      "format": "Format",
      "size": "Size",
      "pages": "Pages",
      "sha256": "Fingerprint (sha256)",
      "hazards": "Active content found",
      "markers": "Markers in the file",
      "scan": "Byte scan of the stored copy",
      "ooxml": "Word or Excel container scan",
      "run": "Check run"
    },
    "notScanned": "Not scanned yet; the release scans it and stops if it finds anything not shown here.",
    "wholeOnly": "This file is added whole; it is never sent through the Bid File Splitter.",
    "viewImages": "View the page images (a safe copy)",
    "imagesPart": "Part {{count}}",
    "noImages": "No page images were kept for this file.",
    "noteLabel": "Why is this file safe to release?",
    "notePlaceholder": "What you checked and what you found",
    "noteRequired": "A note is required.",
    "confirmUnsafe": "I inspected this file and accept the risk of adding it to the project.",
    "confirmUnsafeRequired": "Confirm that you inspected this file.",
    "submit": "Release into the project",
    "done": "Released. The file is being added to the project.",
    "bytesGone": "No copy of this file is available any more; a new harvest is needed."
  }
},
"projectPage": { "rfpFiles": {
  "badge": {
    "unsafe": "Files may be compromised",
    "held": "Files held for IT",
    "rerunnable_one": "{{count}} file needs a re-check",
    "rerunnable_other": "{{count}} files need a re-check",
    "checking": "Checking files"
  },
  "callout": {
    "unsafe_one": "{{count}} file from this RFP was blocked because it may be compromised. IT has been notified. Contact your IT Admin, and do not open this file from the email or the bid portal yourself.",
    "unsafe_other": "{{count}} files from this RFP were blocked because they may be compromised. IT has been notified. Contact your IT Admin, and do not open these files from the email or the bid portal yourself.",
    "held_one": "{{count}} file from this RFP is held by the file safety rules. IT has been notified and can release it after checking. Do not open it from the email or the bid portal yourself.",
    "held_other": "{{count}} files from this RFP are held by the file safety rules. IT has been notified and can release them after checking. Do not open them from the email or the bid portal yourself.",
    "noItAdmin": "No IT Admin could be notified automatically. Tell an Executive so IT can review these files.",
    "itAdminHint": "You are an IT Admin: open View files to inspect and release them.",
    "rerunnable_one": "{{count}} file from this RFP could not be checked yet.",
    "rerunnable_other": "{{count}} files from this RFP could not be checked yet.",
    "checking_one": "{{count}} file from this RFP is still being checked; it is added when the check finishes.",
    "checking_other": "{{count}} files from this RFP are still being checked; they are added when the check finishes.",
    "viewFiles": "View files"
  }
} },
"filesPanel": { "rfpFiles": {
  "notice_one": "{{count}} file from the RFP invitation is not in this list.",
  "notice_other": "{{count}} files from the RFP invitation are not in this list.",
  "viewFiles": "See why"
} },
"biddingLink": { "caution": {
  "title": "Before you open the bidding site",
  "unsafe": "Some files from this invitation were blocked because they may be compromised. Do not download or open those files from the bidding site. Use the files on the project, and contact your IT Admin about the blocked ones.",
  "held": "Some files from this invitation are held for IT review. Do not download or open those files from the bidding site; IT can release them into the project.",
  "listTitle": "Do not open these files from the site:",
  "andMore_one": "and {{count}} more",
  "andMore_other": "and {{count}} more",
  "loadError": "The file list could not be loaded; ask your IT Admin which files are held.",
  "open": "Open the bidding site"
} },
"rfpEmails": { "harvest": { "fileWarning": {
  "unsafe": "Some files from this invitation may be compromised. Do not open them from the email or the bid portal; contact your IT Admin.",
  "held": "Some files from this invitation are held by the file safety rules. Do not open them from the email or the bid portal; IT reviews them once the project is created."
} } },
"rfpCreated": {
  "flags": {
    "filesUnsafe_one": "{{count}} may be compromised",
    "filesUnsafe_other": "{{count}} may be compromised",
    "filesHeld_one": "{{count}} held for IT",
    "filesHeld_other": "{{count}} held for IT",
    "filesRerunnable_one": "{{count}} needs a re-check",
    "filesRerunnable_other": "{{count}} need a re-check",
    "filesChecking": "Checking {{count}}",
    "filesNotUsable": "{{count}} not usable",
    "rechecking": "Re-checking"
  },
  "actions": { "files": "Files", "recheck": "Re-check files" }
},
"rfpProcessing": {
  "stuckKind": { "sandbox_wait": "Waiting for the sandbox" },
  "stuckReason": { "sandbox_wait": "The sandbox has been checking the documents since {{since}}" }
}
```

(The objects above are merges into the existing `projectPage`,
`filesPanel`, `biddingLink`, `rfpEmails.harvest`, `rfpCreated` and
`rfpProcessing` objects, not replacements.) `rfpFiles.keys` keys drop the
leading "/" of marker names (`rfpFileKeyLabel` strips it), because a key
must not start with it by convention here; an `ooxml:<why>` or
`<status>:<code>` key falls back to its raw text through `defaultValue`.
The bidding-site caution uses "the site", the harvest-card link caution
reuses the same modal chrome (the `biddingLink.caution` keys) because
both lead to the invitation's source.

---

## 9. Build plan

A sequential Step 0, then five packages run IN PARALLEL in the same working
tree (no worktrees; the trees hold weeks of uncommitted work: never `git
checkout`, `stash` or `reset`), then a sequential package F. File
ownership is disjoint: a file listed under one package is edited by that
package only. Interfaces between packages are the signatures in sections 3
to 6; call them as specified even before the other package has written
them (tests monkeypatch the other package's functions; F then tests the
chain without mocks). Never edit `tests/test_rfp_email_ingest.py` (its
`FakeDB` is shared): subclass `FakeDB` in your own test file for any
operator you need (`neq` on arrays, `contains`, `range`, `lte`). Never edit
this document; return your build record in your final message (section
12). No em dashes. Dev database only; no migration is applied by a builder
(the orchestrator applies 0136). Run tests with
`bdr_be/.venv/bin/python -m pytest` (a bare `python` is not on PATH).

### Step 0: settings (Sonnet 5, sequential, before everything else)

Files owned: `bdr_be/app/core/config.py`, NEW
`bdr_be/tests/test_rfp_file_settings.py`.

Implements: the six settings of section 11 with their defaults, and a new
`_validate_rfp_file_verdicts` model validator:
`rfp_split_sandbox_wait_minutes >= 5`, `rfp_file_recheck_auto_minutes >=
0`, `rfp_file_recheck_cooldown_seconds >= 0`, `rfp_file_followup_seconds >=
5`, `rfp_file_hold_retention_days >= rfp_ingest_retention_days`,
`rfp_processing_sandbox_wait_minutes >= 1` (each with an env-named
sentence, like config.py:1188-1210). Tests: defaults and every validator.
The parallel packages start only after Step 0's tests pass, so every
package reads real settings.

### Package A: verdicts, promotion pass, notifications (Opus 5.5)

Files owned:
- NEW `bdr_be/app/services/rfp_file_verdicts.py`
- `bdr_be/app/services/rfp_create_files.py`
- `bdr_be/app/services/rfp_create.py`
- `bdr_be/app/services/notification_email.py`
- `bdr_be/app/services/notifications.py`
- NEW `bdr_be/supabase/migrations/0136_rfp_file_verdicts.sql`
- NEW `bdr_be/tests/test_rfp_file_verdicts.py`
- `bdr_be/tests/test_rfp_create_files.py`, `bdr_be/tests/test_rfp_create.py`,
  `bdr_be/tests/test_notification_email.py`, `bdr_be/tests/test_notifications.py`

Implements: section 3 (the whole module, every sentence verbatim), 4.1,
4.2, 4.3 (the SQL verbatim), 5a (including `scan_file`, `store_scan`,
`live_file`, `session_frozen`, `request_pass`, `request_pass_for_runs`,
`enqueue_pass`, the release branch, `annotate_entries`), 5g, the A parts
of 5j, section 7 (including `notify_role -> int`).

Calls into B: `rfp_split.stage_late`, `REFUSED_OVER_CAP`,
`REFUSED_WHOLE_ONLY`, `may_stage`, `promote_split_file(release_note=)`,
`rfp_split.enabled`. Calls into C: `rfp_file_rescue.run_recheck`,
`RecheckCandidate` (imported inside the function: C imports A at module
level).

Tests (key cases):
- `test_rfp_file_verdicts.py`: the ten rules of 3.2 with the escalation
  cases; worst-key precedence over hazard, scan and OOXML keys; entry-level
  vs file-level `rejected`; zip entries and skipped members; legacy entries
  (error sentence -> code); expiry suffix and flags; `may_recheck` false
  under an active run (F4); `not_pdf` magic and extension split; X1a / X1b;
  released precedence (P12, a released file with a download Skip shows the
  P row); `clean_name` (control characters, U+2028, an RLO name, zero-width
  characters, NFC, cap); `scan_of` sha binding; `live_keys`;
  `provisional_red_markers` ignores `/AA`; `sort_and_cap` order and cap;
  `counts` ignores `listed = False`; `legacy_tier`; `splitter_ok`
  (released OOXML key -> False); the three message builders (10 quoted
  names, "and N more", zero parts omitted, plural);
  `release_note` / `handoff_note` kinds in Pacific.
- `test_rfp_create_files.py`: the claimed row is what the pass reads
  (stale pre-claim values ignored); the enriched `files_skipped` across two
  harvests keyed by sandbox id; the count columns, `files_recheckable`,
  `hold_run_ids`, `files_wait` and `files_followup_at`; a person-deleted
  promoted file is P11 and never re-promoted, segments included; S1 only
  for files not already on the project; an AMBER hazard PDF is scanned once
  (download, `store_scan`) and escalates to unsafe with `/JS`, and the next
  pass does not download it; IT alert: notify first, record only when
  `notify_role` returned > 0, zero recipients leave `files_unalerted`, an
  escalation (held -> unsafe) re-alerts, a second pass does not, frozen
  records never alert; release gating per format (a released PDF with an
  extra marker is P12; a released hazard-held `.docx` whose container has
  `vbaProject` lands as the converted PDF; a released `.doc` never lands as
  `.doc`; a released `crash_loop` is promoted whole, never passed to
  `stage_late`; a second hazardous unreleased file stays skipped); late
  eligibility without `previous` (a file pending at split time and
  verified before the first pass goes to `stage_late`); `over_cap` and
  `released_whole_only` refusals promoted whole; in-flight late row -> S1
  and `files_wait.split_jobs`; failed late row -> whole fallback;
  hand-off locked (read false then true mid-pass) -> `additional` with the
  note by kind, no `promote_split_file`, the Estimating Admin bell with the
  kind counts; drawings bell for rows the resync filed since the last
  pass; re-check state machine (auto `due` -> started -> `running` ->
  `done` + bell once; transient -> `scheduled` again, tries capped; final
  refusal -> `done` + bell; manual request handled and cleared; a request
  arriving mid-pass survives; `recheck_auto_at` null outside `scheduled`);
  dismissals; `enqueue_pass` (marker first; busy record; pending with and
  without an active job; lost mark; JobAlreadyActive; other exceptions do
  not raise); `request_pass_for_runs`; `retryable` narrowed (the :937 test
  flips); `annotate_entries` (scan-based and provisional verdicts);
  `run_statuses`; `project_harvests` order and dedupe (the :642 test is
  updated to the all-harvests contract); `test_migration_0136_*` asserting
  the SQL text (columns, CHECKs, indexes, `notify pgrst`).
- `test_rfp_create.py`: a harvest whose every entry is `rejected` still
  enqueues the promotion job; `_link_files_job` on a busy record calls
  `request_pass` and writes the new `_MSG_FILES_BUSY`.
- `test_notification_email.py`: headings for the three types; linkify off.
- `test_notifications.py`: `notify_role` returns the row count, 0 for no
  active user and for the estimator refusal.

### Package B: sandbox, split, queue (Opus 5.5)

Files owned:
- `bdr_be/app/services/rfp_split.py`
- `bdr_be/app/services/rfp_ingest.py`
- `bdr_be/app/services/rfp_ingest_storage.py`
- `bdr_be/app/services/llm_queue.py`
- `bdr_be/app/services/pdf_split.py`
- `bdr_be/tests/test_rfp_split.py`, `bdr_be/tests/test_rfp_ingest.py`,
  `bdr_be/tests/test_rfp_ingest_storage.py`, `bdr_be/tests/test_llm_queue.py`,
  and whichever existing test pins `pdf_split`'s unreadable message (grep;
  `tests/test_pdf_combine.py` pins pdf_combine's own, not this one)

Implements: 5b (`may_stage`, `StageRefused`, `REFUSED_WHOLE_ONLY`,
`context_for_harvest`, the deadline with its once-only stamp), the B parts
of 5c (`stage_late` with `renew`, `LateStage`, `late_job_state`,
`REFUSED_OVER_CAP`, the sweep thread, the `resync_project_files` harvest
preference and `release_note`, `promote_split_file(release_note=)`), 5d
(`retry_run(file_ids)`, `RfpIngestBusy`, `reset_file_ids`,
`prior_fail_code`, `object_exists`), `rfp_ingest.cancel_if_unowned` with
its grace, 5h, the B events of 5j, and `pdf_split._readable_reader`
(pdf_split.py:25-41): the `({exc})` suffix becomes the app-authored "The
PDF could not be read." and the exception is logged server side.

Calls into A: `rfp_create_files.promotion_for`, `fetch_entry`,
`file_rows`, `run_statuses`, `request_pass_for_runs`,
`rfp_file_verdicts.splitter_ok`. Calls into C:
`rfp_file_rescue.followup_tick` (the sweep thread only).

Tests (key cases):
- `test_rfp_split.py`: the wait stamp is written once and reused; before the
  deadline it waits; at the deadline only the stamp's CAS winner cancels an
  unowned active run and records the event (a second poll after the
  deadline does neither); an owned run is not canceled; the step stages the
  verified entries and proceeds; real `STATUS_REJECTED` / `STATUS_FAILED`
  rows through `advance` (refused, `no_files`); a released `crash_loop`
  file listed by a later unlinked harvest (`reused`) is NOT staged by
  `_start` (no download, `released_whole_only`) and `_stage_row` raises
  `StageRefused` for it; `stage_late` reuses `split_job_id` (including a
  zero-file failed job, then `refresh_job` flips it to processing), creates
  and links a job when none (harvest CAS, `project_id`), skips sids
  already staged and reads a unique violation as staged, refuses past the
  cap with `over_cap` and a whole-only release with `released_whole_only`,
  calls `renew` per item and stops on its exception, uses
  `context_for_harvest`, never touches `split_status`; `late_job_state`
  running / outage wait / requeue / done / missing; `resync_project_files`
  picks `job.rfp_harvest_id` and appends the release note to segments and
  the source set.
- `test_rfp_ingest.py`: `retry_run(file_ids)` resets only listed failed,
  pending, running and gapped files, leaves rejected and unlisted rows,
  admits a `done` run, 409s before the CAS when nothing listed is
  resettable, returns `reset_file_ids`, writes `prior_fail_code` on every
  reset (both paths), raises `RfpIngestBusy` for the active-job and
  changed-run cases, and the dev path (`file_ids=None`) is otherwise
  unchanged (existing tests :1976-2066 still pass); `cancel_if_unowned`
  (owned, unowned inside the grace, unowned past it, grace 0, moved run);
  `prune_expired` keeps held runs inside the cap, expires held runs past
  it, pages past kept rows, does not skip rows after a lost CAS, and asks
  for a pass for projects whose held run expired.
- `test_rfp_ingest_storage.py`: `object_exists` 200 / 206 / 404 / 400
  not-found body / 500.
- `test_llm_queue.py`: the follow-up thread starts at most once per
  interval, only with `rfp_ingest_enabled`, never while the previous one
  still holds the lock, never blocks `_sweep` (a slow `followup_tick` does
  not delay the return), and an exception inside it releases the lock.
- the pdf_split message test: the unreadable sentence carries no exception
  text.

### Package C: rescue service and APIs (Opus 5.5)

Files owned:
- NEW `bdr_be/app/services/rfp_file_rescue.py`
- `bdr_be/app/routers/rfp_created.py`
- `bdr_be/app/routers/projects.py`
- `bdr_be/app/routers/bid_splitter.py`
- `bdr_be/app/models/schemas.py`
- `bdr_be/app/services/rfp_processing.py`
- `bdr_be/app/routers/rfp_processing.py`
- `bdr_be/app/routers/rfp_testing.py`
- NEW `bdr_be/tests/test_rfp_file_rescue.py`
- `bdr_be/tests/test_rfp_created_router.py`, `bdr_be/tests/test_rfp_processing.py`,
  `bdr_be/tests/test_rfp_testing.py`, `bdr_be/tests/test_bid_splitter.py`,
  and whichever existing test pins `_rfp_created_summary` /
  `RfpCreatedSummary` (grep before editing; if it lives in a file another
  package owns, add a new test file instead)

Implements: `RescueRefused`, `RecheckCandidate`, `RecheckOutcome`,
`request_recheck`, `run_recheck`, `release`, `inspect`, `files_view`,
`recheck_state`, `followup_tick` (5c to 5f), the routes and payloads of
section 6 (rate limits, `_uuid_or_404` on both ids, 503 without the
queue), `RfpFileReleaseIn`, the `RfpCreatedSummary` fields, the
`bid_splitter` hand-upload guard (5c), 5i backend, the C events of 5j, the
bench flags.

Calls into A: `rfp_file_verdicts.*`, `rfp_create_files.project_harvests`,
`file_rows`, `run_statuses`, `promotion_for`, `live_file`, `scan_file`,
`store_scan`, `session_frozen`, `request_pass`, `request_pass_for_runs`,
`enqueue_pass`, `active_job`, `claim_is_stale`. Calls into B:
`rfp_ingest.retry_run(file_ids=...)`, `RfpIngestBusy`,
`rfp_ingest.cancel_if_unowned`, `rfp_ingest.get_file`,
`rfp_ingest.file_urls`, `rfp_ingest_storage.object_exists`,
`rfp_split.late_job_state`.

Tests (key cases):
- `test_rfp_file_rescue.py`: `request_recheck` every refusal with its exact
  sentence and status (503 queue off and sandbox off, scheduled with the
  Pacific time, running / requested / runs, cooldown, expired, nothing
  waiting, lost CAS), the CAS writes the pass marker in the same update,
  `enqueue_pass` called, audit; `run_recheck` groups by run and passes
  `file_ids`, `gone` for a missing object and a null path, a storage error
  -> transient with nothing started, `RfpIngestBusy` -> transient, a
  permanent refusal is not transient, an enqueue failure (no active job
  after `retry_run`) -> `cancel_if_unowned(grace_seconds=0)` and transient,
  `request_pass_for_runs` for other projects; `followup_tick` steps 1 to 3
  including the lost-CAS paths, the atomic marker writes, the stranded-run
  cancel, frozen sessions skipped (an ended-session record with
  `recheck_auto='scheduled'` does nothing and sends no bell);
  `release` every refusal of 5f, the reviewed-keys mismatch, a scan that
  finds a new marker (scan stored, pass requested, 409 naming it), the
  CAS, re-release of a `released:changed` file, other projects asked for a
  pass, an audit insert failure does not 500, and no quarantine URL
  anywhere in `inspect`; `files_view` overlays (expired run, release,
  legacy entries) and `recheck_state` (`auto_at` only while scheduled).
- `test_rfp_created_router.py`: GET files for every internal role and 403
  for the estimator; recheck 403 for engineers and accountant, 202 for
  PAGE_ROLES, 409 / 503 mapping, `ai_rate_limit` wired; inspect and release
  403 for `executive` and `estimating_admin`, 202 / 200 for `it_admin`, 404
  for a malformed `sandbox_file_id`, 422 without `confirm_unsafe` on an
  unsafe file; the list's new flags; retry-files 409 on a complete record
  with the new sentence.
- `test_bid_splitter.py`: a hand upload into a `source = 'rfp'` job is
  409 with the sentence; a manual job still accepts uploads.
- `test_rfp_processing.py`: `sandbox_wait` for an email row and a portal
  row at `split` past the threshold, not before, not at other statuses,
  and counted in `summarize`.
- `test_rfp_testing.py`: the new flags and `files_counts`.

### Package D: frontend (Opus 5.5)

Files owned: NEW `bdr_fe/lib/rfpFileVerdicts.ts`, NEW
`bdr_fe/components/RfpFileVerdictsModal.tsx`, NEW
`bdr_fe/components/RfpFileReleaseModal.tsx`, NEW
`bdr_fe/components/RfpFileWarning.tsx`, and
`bdr_fe/app/(app)/projects/[id]/page.tsx`, `bdr_fe/components/FilesPanel.tsx`,
`bdr_fe/components/BiddingLinkButton.tsx`, `bdr_fe/components/GoNoGoPanel.tsx`,
`bdr_fe/components/PricingPanel.tsx`, `bdr_fe/components/GoNoGoModal.tsx`,
`bdr_fe/app/(app)/rfp-created/page.tsx`, `bdr_fe/lib/rfpCreated.ts`,
`bdr_fe/components/RfpHarvestBlock.tsx`, `bdr_fe/lib/rfpEmails.ts`,
`bdr_fe/lib/rfpPortal.ts`, `bdr_fe/lib/rfpProcessing.ts`,
`bdr_fe/app/(app)/rfp-processing/page.tsx`, `bdr_fe/lib/rfpTesting.ts`,
`bdr_fe/app/(app)/rfp-testing/tabs.tsx`.

Implements: section 8.1, 8.2, 5i FE. Uses the i18n keys of 8.3 exactly
(package E writes the catalogs; until then `t()` shows the key).

Gates: in `bdr_fe`, `npx tsc --noEmit` and `npx eslint .` clean (strict
react-hooks; `next lint` is gone). Read
`node_modules/next/dist/docs/` for anything Next-specific (bdr_fe/AGENTS.md).
Never run `npm run build` beside a running `next dev`.

### Package E: catalogs, docs, harvester entries (Sonnet 5)

Files owned: `bdr_fe/locales/{en,hi,ceb,fil,sw,ur}/translation.json`;
`bdr_be/docs/RFP_CREATE.md`, `RFP_SPLIT.md`, `RFP_INGESTION_SANDBOX.md`,
`RFP_PROCESSING.md`, `RFP_TESTING.md`, `RFP_HARVEST.md`;
`bdr_be/app/services/rfp_harvest.py`, `bdr_be/app/services/rfp_portal_ingest.py`,
`bdr_be/app/services/rfp_email_harvest.py`;
`bdr_be/tests/test_rfp_harvest.py`, `bdr_be/tests/test_rfp_portal_ingest.py`,
`bdr_be/tests/test_rfp_email_harvest.py`.

Implements:
- 8.3: every key in all six catalogs, merged into the existing objects,
  English values verbatim in `en` and in the `rfpProcessing` additions of
  every catalog, hand translations for the rest (hi, ceb, fil, sw, ur),
  placeholders verbatim, no em dashes. A script in the scratchpad loads each
  catalog, merges, and writes `json.dumps(obj, ensure_ascii=False, indent=2)
  + "\n"`; afterwards a check that every new key exists in all six and that
  a re-dump is byte-identical.
- 4.4: the intake `reject_code` / `sniff_magics` writes in both harvesters
  and the zip `reject_code` in the email harvester; the harvest-card call
  to `rfp_create_files.annotate_entries` in `harvest_for_email` and
  `harvest_for_invitation` (best effort, a raise is logged). Tests: an
  intake reject records both fields; a bomb and a not-zip record their
  code; both detail readers call `annotate_entries` (patched) and survive
  it raising.
- Docs, short cross-references to this contract (no duplication):
  RFP_CREATE.md sections 5 (the pass walks every harvest; tiers; enriched
  `files_skipped`; in-pass re-check; `promoted_ids`; `additional` after the
  hand-off), 7 (the three new bells), 8 (the routes of section 6; fix the
  drift at :682-686 and :873 about Retry documents), 10 (the settings);
  RFP_SPLIT.md (the deadline, late staging, `may_stage`, `late_job_state`,
  the rfp-job upload guard); RFP_INGESTION_SANDBOX.md (`retry_run`
  `file_ids`, `prior_fail_code`, `cancel_if_unowned`, the release and scan
  columns, the retention exemption, the quarantine never served, updating
  :86-88 accordingly); RFP_PROCESSING.md (`sandbox_wait`); RFP_TESTING.md
  (the events of 5j, the frozen rule, a live checklist line for held
  files); RFP_HARVEST.md (the entry `reject_code` / `sniff_magics`, the zip
  codes and the card verdicts).

Dependencies: none on code beyond calling A's `annotate_entries`; keys and
sentences are fixed here.

### Package F: cross-package flow tests (Opus 5.5, sequential after A to E)

Files owned: NEW `bdr_be/tests/test_rfp_file_flow.py` (a `FakeDB` subclass
of its own; only storage, the sandbox runner and the queue's enqueue are
stubbed; no package function is mocked).

Drives, end to end over the real modules: a harvest with a rejected, a
failed and a verified file -> create -> pass (classification, IT alert,
automatic re-check scheduled) -> sweep step 1 -> pass runs `run_recheck`
-> `retry_run(file_ids)` -> the run ends -> sweep step 2 -> pass ->
`stage_late` -> split row done -> `resync_after_run` -> next pass (drawing
bell, `promoted_ids`); the deadline with an unowned run -> canceled ->
rerunnable -> automatic re-check; an IT release of a RED and an AMBER file
-> the splitter (verified) and whole (`crash_loop`); the post-hand-off
landing as `additional` with the right note kinds; split flags off ->
checking -> follow-up -> `category_for`; an expired run -> buttons off; a
deleted file never returns. A gate before the live test.

---

## 10. Test plan

### 10.1 Unit tests per package

As listed under Step 0 and each package in section 9; package F is the
integration gate.

### 10.2 Regression suites (run all after the packages land)

```
cd bdr_be && .venv/bin/python -m pytest -q \
  tests/test_rfp_file_verdicts.py tests/test_rfp_file_rescue.py tests/test_rfp_file_settings.py \
  tests/test_rfp_file_flow.py \
  tests/test_rfp_create_files.py tests/test_rfp_create.py tests/test_rfp_created_router.py \
  tests/test_rfp_split.py tests/test_bid_splitter.py tests/test_bid_split_corrections.py \
  tests/test_rfp_ingest.py tests/test_rfp_ingest_router.py tests/test_rfp_ingest_storage.py \
  tests/test_rfp_sandbox_runner.py tests/test_rfp_sandbox_child.py tests/test_rfp_sanitize.py \
  tests/test_rfp_harvest.py tests/test_rfp_email_harvest.py tests/test_rfp_email_files.py \
  tests/test_rfp_portal_ingest.py tests/test_rfp_portal_router.py tests/test_rfp_emails_router.py \
  tests/test_rfp_email_ingest.py tests/test_rfp_processing.py tests/test_rfp_testing.py \
  tests/test_llm_queue.py tests/test_notification_email.py tests/test_notifications.py \
  tests/test_notification_log.py tests/test_file_updates.py tests/test_estimator_rounds.py \
  tests/test_project_redaction.py tests/test_projects_rfp_matches.py tests/test_pdf_combine.py
```

Then the full suite once (`.venv/bin/python -m pytest -q tests/`, about
5000 tests on 2026-09-23).

FE gates: `cd bdr_fe && npx tsc --noEmit && npx eslint .`

### 10.3 Live dev test (BDR dev only; confirm `.active-db` says dev first)

Steps marked APPROVAL need the user's explicit go-ahead before they run.
Every step is mandatory unless it says otherwise.

1. APPROVAL: apply migration 0136 to dev BDR (after the pre-check query of
   4.3), `notify pgrst`. Never BDR_Prod.
2. APPROVAL: env for the run in `bdr_be/.env` (dev): keep
   `BID_FILE_SPLITTER_ENABLED=true`, `RFP_SPLIT_ENABLED=true`; set
   `RFP_FILE_RECHECK_AUTO_MINUTES=2`, `RFP_FILE_FOLLOWUP_SECONDS=10`,
   `RFP_SPLIT_SANDBOX_WAIT_MINUTES=5` (the validator minimum),
   `RFP_PROCESSING_SANDBOX_WAIT_MINUTES=1`; restart the backend.
3. APPROVAL: the bench session. Session 9e1bfb49 (symone) has been active
   since 2026-09-21 (memory); either reuse it or end it and start a fresh
   one (`POST /rfp-testing/sessions`). A session must stay active for the
   whole test so every notification mirror is redirected (section 7);
   bells for a record of an ended session are suppressed by design.
4. Fixtures, built in the scratchpad with pypdf (modeled on the in-memory
   builders at tests/test_rfp_sandbox_child.py:96-112, 293-330; harmless
   content only, never exploit-class samples, RFP_INGESTION_SANDBOX.md:
   83-85). Benign-looking ones travel as email attachments:
   - `encrypted.pdf` (`writer.encrypt("x")`): expect R1 not_usable.
   - `gotor_link.pdf` (a Link annotation with a `/GoToR` action to a local
     name): expect H6 held, IT alert, releasable, staged into the splitter
     after release.
   - `aa_widget.pdf` (a form field whose widget carries `/AA` with a GoTo
     action): expect M8 held after the download marker scan.
   - `open_page2.pdf` (`/OpenAction [page 2 /Fit]`, no script): expect M7
     held.
   - one ordinary spec sheet copied from `subs/`: expect promoted.
   - one `.docx` (a copy of the Proposal template in `example_files/`):
     used for the FAIL test in step 8.
   Hostile-looking ones (they could trip Microsoft Defender or Exchange
   Online Protection on the owner's account, and the dev machine is not a
   security boundary for the sandbox child, RFP_INGESTION_SANDBOX.md:
   82-85) travel ONLY through a Dropbox share link in the email body (the
   email harvester downloads supported links, cloud_folders.py:106-117):
   - `js_alert.pdf` (an `/OpenAction` JavaScript `app.alert("test")`):
     expect M1/M2 unsafe (or H1), IT alert, releasable with the confirm.
   - `js_page_action.pdf` (a page `/AA /O` JavaScript action): expect
     `hazard:page_actions` escalated to unsafe by the scan (D1f).
   - `polyglot.pdf` (PNG magic `\x89PNG\r\n\x1a\n` prepended to a small
     PDF): expect E1 unsafe, IT alert, no release button.
   - `mz_named.pdf` (the text "MZ" plus a line of text, no PDF header) and
     `note.js` (a one-line comment): expect E2 unsafe (magic, then
     extension).
   APPROVAL: the user creates the Dropbox folder and share link (the
   assistant never uploads to the user's cloud accounts).
5. APPROVAL: send email 1 from the accepted sender (t.moorejr) to the bench
   mailbox (symone) with the benign attachments and the share link. Create
   the project with the bench's `auto_create` (APPROVAL: toggling it) or
   press Create project (`RFP_CREATE_AUTO_ENABLED` is off).
6. Verify: before creation, the harvest card's Callout, inline messages and
   provisional marks, and the caution step on its "Open link"; /rfp-created
   badges and the Files modal (tiers, sentences, keys); the project header
   badges and Callouts; FilesPanel's notice; the BiddingLinkButton caution
   modal on Go/No-Go listing the held names; the IT Admin bell (one row,
   the quoted file list) and its redirected mirror email; a second
   promotion pass does NOT alert again; the bench Projects tab and events;
   `rfp_created_projects` columns, `promotion_scan` on the AMBER files and
   `files_skipped` on dev. As an engineer (materials or labor) account: the
   warnings show, the Re-check and Release buttons do not (screenshot).
7. Split flags off: APPROVAL: set `RFP_SPLIT_ENABLED=false`, restart, send
   email 2 with two ordinary PDFs; create at once; expect the files
   `checking`, then promoted whole via `category_for` by the follow-up
   pass; restore the flag and restart.
8. Real FAIL: APPROVAL: `docker stop bdr-gotenberg` while the `.docx` is
   being converted (email 3 with only the `.docx`), expect X7
   `failed:conversion_unavailable`, rerunnable, the project created without
   it, the automatic re-check scheduled; APPROVAL: `docker start
   bdr-gotenberg` before it fires; expect the automatic re-check to verify
   it, the follow-up pass to stage it late into the harvest's split job,
   and the file to land on the project; the Re-check button appears only if
   something is still rerunnable.
9. Whole-run FAIL: APPROVAL: set `RFP_INGEST_SCRATCH_RESERVE_MB` above the
   machine's free disk and restart; send email 4; expect every file
   `failed/storage`, the run `failed`, the project created with zero files,
   everything rerunnable; let the automatic re-check fire while the
   setting is still wrong; expect it to fail again, `recheck_auto = done`
   and the `rfp_create.files_recheck_needed` bell (redirected mirror).
   APPROVAL: restore the setting and restart; press Re-check files; expect
   the files promoted.
10. Deadline, owned run (D3a): occupy the single sandbox slot
    (`rfp_ingest_sandbox_concurrency` 1, config.py:347) with a long
    `/ingestion-sandbox` run of a harmless 400-page PDF, then send email 5;
    expect the split row to wait, `sandbox_wait` on /rfp-processing after 1
    minute, the deadline after 5 minutes (one bench event), no cancel, the
    project created with the files `checking`, and, when the slot frees,
    the follow-up staging them late through the splitter.
11. Deadline, unowned run (D3): send email 6 while the slot is occupied
    again; APPROVAL: mark that run's queued `llm_jobs` row `canceled` by SQL
    on dev (the run then has no owner); expect the deadline to cancel the
    run, the files `rerunnable`, the project created, and the automatic
    re-check to verify them once the slot frees.
12. Release: as the IT Admin (dev account with role `it_admin`), open
    `gotor_link.pdf` in the Files modal, Inspect, write a note, Release;
    expect the audit row, the release columns, the file staged into the
    split job and its segments on the project carrying the release note.
    Release `js_alert.pdf` with the confirm checkbox; expect it promoted
    through the splitter (verified). Try to release `js_page_action.pdf`
    with stale `reviewed_keys` (reload the modal only after the scan);
    expect the 409 naming the new finding.
13. Post-hand-off (D4): APPROVAL: assign an estimator and send the package
    (all mail redirected by the session); then release `aa_widget.pdf` and
    rescue a `.docx` FAIL (step 8's pattern); expect both under Additional
    files with their notes ("released by IT", "cleared by the file safety
    check"), unsent, and the Estimating Admin bell with the kind counts.
14. Simulated expiry: APPROVAL: `update rfp_ingest_runs set status =
    'expired' where id = <a held run>` on dev; request a pass (press
    Re-check or wait for the follow-up); expect the files view and the
    project Callout to disable Re-check and Release with "a new harvest is
    needed".
15. Headless screenshots of the project banner, the Files modal, the
    release modal, the harvest card and /rfp-created (Playwright cookie
    injection with the smoketest-claude account, memory
    bdr-headless-ui-screenshots), plus the engineer view of step 6.
16. Cleanup: end the session, then `POST /rfp-testing/sessions/{id}/cleanup`
    (APPROVAL, it deletes the session's rows); restore every env change and
    restart (APPROVAL); the user deletes the Dropbox folder.

Not provable live on this machine: memory-limit failures (no RLIMIT_AS on
macOS, config.py:1045-1049, rfp_sandbox_runner.py:52-54).

---

## 11. Release steps (dev now; staging and prod later, only with approval)

Dev (now):

1. Step 0 and the package builds land; package F and section 10.2 green;
   FE gates green.
2. Pre-check query (4.3) returns nothing on dev; apply
   `0136_rfp_file_verdicts.sql` to dev BDR; `notify pgrst, 'reload
   schema'` (in the file). The migration must be applied BEFORE the
   backend that reads the new columns starts (the projects select and the
   promotion pass reference them).
3. Env (defaults shown; nothing is required to be set):

| Env | Setting | Default | Meaning |
|---|---|---|---|
| `RFP_SPLIT_SANDBOX_WAIT_MINUTES` | `rfp_split_sandbox_wait_minutes` | 60 | The split step's sandbox wait deadline (min 5). |
| `RFP_FILE_RECHECK_AUTO_MINUTES` | `rfp_file_recheck_auto_minutes` | 10 | The one automatic re-check delay; 0 disables it. |
| `RFP_FILE_RECHECK_COOLDOWN_SECONDS` | `rfp_file_recheck_cooldown_seconds` | 120 | A manual re-check refuses within this long of the last one. |
| `RFP_FILE_FOLLOWUP_SECONDS` | `rfp_file_followup_seconds` | 30 | The follow-up sweep interval (min 5). |
| `RFP_FILE_HOLD_RETENTION_DAYS` | `rfp_file_hold_retention_days` | 60 | Hard cap for keeping held runs' bytes, from the run's creation (>= `RFP_INGEST_RETENTION_DAYS`). |
| `RFP_PROCESSING_SANDBOX_WAIT_MINUTES` | `rfp_processing_sandbox_wait_minutes` | 20 | /rfp-processing flags a split row stuck (`sandbox_wait`) after this. |

   Code constants, not settings: `_UNOWNED_GRACE_SECONDS = 300`
   (rfp_ingest), `_AUTO_RECHECK_RETRY_SECONDS = 300` and
   `_AUTO_RECHECK_MAX_TRIES = 6` (rfp_create_files), `SKIPPED_CAP = 300`.
4. Flags unchanged: the slice runs wherever `RFP_INGESTION_ENABLED` and the
   RFP intake run; the split-related parts need `BID_FILE_SPLITTER_ENABLED`
   and `RFP_SPLIT_ENABLED`; re-check and release need `LLM_QUEUE_ENABLED`;
   no new flag (B4).
5. Restart the backend and the FE dev servers.

Staging and prod (later, with the user's explicit approval only): bundle
0136 with the RFP ingestion migrations 0119 to 0135 (all dev only today,
memory), run the 4.3 pre-check there, apply, then deploy the backend, then
the FE. Railway env: none required (defaults); set
`RFP_FILE_RECHECK_AUTO_MINUTES` only to change the delay. Records created
before 0136 have an empty `promoted_ids`: their first pass after the deploy
may re-add a file a person had deleted before (once), which the release
notes should mention.

---

## 12. Build records

(Empty. The orchestrator appends one subsection per package from each
builder's final report: what was built, deviations from this contract with
the reason, tests added and their counts, open items.)

---

## 13. Open questions

1. Intake `download_failed` entries that carry a `sandbox_file_id` (D1e):
   the code keeps no bytes for them (the quarantine upload is what failed,
   rfp_ingest.py:939-956), so this contract classifies them `not_usable`
   ("a new harvest is needed") instead of D1's `rerunnable`. Confirm, or ask
   for a later "download again" action (a targeted re-harvest of one file),
   which is new harvester work outside this slice.
2. IT inspection is limited to the sandbox's safe outputs (D2b): the facts
   (now including the byte-marker and OOXML scans), the page-images PDF,
   the text and the manifest. The raw original is never served because
   `rfp_ingest_storage.signed_url` refuses the quarantine bucket by design
   (rfp_ingest_storage.py:500-510), so IT cannot see link TARGETS (for
   example a GoToR or GoToE path that points at a UNC share, D1c). For a
   file with no derived outputs (`crash_loop`, a repeated
   `invalid_output`, OOXML refusals) the IT Admin can only release on the
   facts or fetch the file from the source on an isolated machine. Confirm
   that is acceptable, or decide whether an audited IT-only original
   download (breaking that invariant) is wanted.
3. D3a: at the 60 minute deadline, a run that a queue job still owns (slow
   but alive) is NOT canceled; its files stay `checking` with no button
   until the run ends (at worst the run's retry ladder, after which they
   become `rerunnable` and the automatic re-check follows). D3 said "every
   unfinished file is marked rerunnable". Confirm the refinement, or
   choose to cancel owned runs at the deadline too (the running job then
   stops mid-file).
4. Before a project exists nobody is alerted: the IT alert comes from the
   first promotion pass after creation, and with `RFP_CREATE_AUTO_ENABLED`
   off (config.py:716) a row can sit at `done`, or be declined at No-Go,
   with a RED file in a real mailbox. The harvest card now warns the
   reviewer (DF4). Confirm, or ask for a pre-creation IT alert (it needs a
   new trigger when a harvest's sandbox run finishes, which this slice
   does not add).

---

## 14. Review log

Three adversarial reviews (lenses code-correctness, safety-security,
completeness) of the 2026-09-24 draft. Each finding was checked against the
code; ids are `lens#index` in the order the reviews listed them.

| Finding | Verdict | Reason |
|---|---|---|
| code-correctness#1 | fixed | Verified (rfp_create_files.py:13-15, 300-304, 318-319): a release now lifts only the reviewed findings; `.doc` / `.xls` always promote the converted PDF, `.docx` / `.xlsx` keep the OOXML scan and fallback, the inspect payload shows the scan (D2d, 5a, 5f). |
| code-correctness#2 | fixed | Verified: the draft ended the automatic retry on any refusal and `may_recheck` ignored active runs. Re-checks now run inside the pass (D3d); transient refusals reschedule (max 6); `may_recheck` needs a terminal run and failed files under an active run are F4 `checking`; a final refusal bells (5a step 11). |
| code-correctness#3 | fixed | Verified (hazards.py:15-31; the hazard Skip precedes any download). AMBER hazard files are scanned once (`scan_file`, stored in `promotion_scan`) and tiered over hazard plus marker keys (D1f, 5a step 5.4, 3.2 rule 10). |
| code-correctness#4 | fixed | Verified (files.py delete keeps no RFP bookkeeping). `promoted_ids` on the record; a promoted file whose rows are gone is P11 and never re-promoted, segments included (B5, 5a steps 5.2, 5.3, 5.6). |
| code-correctness#5 | fixed | The window disappears: the sweep only CASes `scheduled -> due` with the pass marker in one write, and every later state change happens inside the claimed pass; manual requests are refused while the automatic one is scheduled, due or running (5c, 5d, 5e). |
| code-correctness#6 | fixed | Verified (`_start` stages only what promotes then, rfp_split.py:709-722). Late eligibility no longer reads `previous`: split ran, no split row, `may_stage` and `splitter_ok` (5a step 5.9). |
| code-correctness#7 | fixed | Verified (rfp_create.py:1264-1273, 149-152; `_attach_harvest` runs first, :1321-1322). The busy branch calls `request_pass` and the sentence no longer points at Retry documents (5a). |
| code-correctness#8 | fixed | `recheck_auto_at` is cleared by the `due` CAS and a CHECK ties it to `scheduled` (4.3); summary and list serve `recheck_available` and `recheck_auto`, the FE gates on them (6, 8.1). |
| code-correctness#9 | fixed | Verified (rfp_email_harvest.py:523-571). `request_pass_for_runs` after a re-check starts runs and after a release (GIN index on `hold_run_ids`); the in-pass re-check classifies live, so stale stored entries never cause a refusal loop (5d, 5f). |
| code-correctness#10 | fixed | Every hand-off now sets `files_pass_requested_at` in the same write that clears its own marker, and `enqueue_pass` never raises; a stranded re-check run (enqueue failed, rfp_ingest.py:1016-1021) is canceled by `cancel_if_unowned` (5c step 2, 5d). |
| code-correctness#11 | fixed | Verified (rfp_create_files.py:879, 895; lease 900 s, config.py:218). `stage_late` takes `renew` and calls it per item (5c). |
| code-correctness#12 | fixed | Verified (rfp_split.py:982-988). The "already on the project" check runs before S1 (5a steps 5.2 and 5.5). |
| code-correctness#13 | fixed | Verified: no such function exists. `rfp_split.context_for_harvest(harvest)` is declared (5b). |
| code-correctness#14 | fixed | Verified (estimator.py:140-145). `locked` is re-read before each landing decision while false; the lock is monotonic (5a step 2, 5g). |
| code-correctness#15 | fixed | Verified (llm_queue.py:1017-1019). The sweep starts `followup_tick` on a daemon thread behind a non-blocking lock, and `followup_tick` does no downloads or sandbox work (re-checks moved into the pass) (5c). |
| code-correctness#16 | fixed | Verified (rfp_create_files.py:827-832). `_claim` returns the claimed row and the pass reads only it (5a step 1). |
| code-correctness#17 | fixed | Verified (rfp_split.py:1223-1277 rings nothing). The pass counts drawing rows the resync filed since the last pass and rings `_notify_drawings` (5a step 5.2). |
| code-correctness#18 | fixed | New `rfp_harvests.split_sandbox_deadline_at`; only its CAS winner cancels and records the event (4.3, 5b step 4). |
| code-correctness#19 | fixed | Verified (rfp_ingest.py:3072-3073). The offset advances only by rows kept for the hold; a lost CAS counts as neither (5h). |
| code-correctness#20 | fixed | Verified (main.py:138-150). Re-check and release answer 503 without the queue (5d, 5f, 6, B4). |
| code-correctness#21 | fixed | Verified (postgrest-py `eq` sends the Python repr; `contains` formats `{a,b}`). No array compare-and-set remains; the array rule is stated in 4.3. |
| code-correctness#22 | fixed | Released files are classified by reason before status (3.1 step 3); re-release is allowed for P12. |
| safety-security#1 | fixed | Verified (rfp_split.py:709-722, 606-608). `may_stage` is enforced inside `_stage_row` (raises `StageRefused`) and checked by `_start` and `stage_late` before downloading; D4a no longer invites whole-only files into the splitter (D2e, 3.4). The suggested sha256 refusal of manual splitter uploads is not taken: a hand upload never passed the sandbox in the first place. |
| safety-security#2 | fixed | Same root as code-correctness#1: the release re-scans, must match the reviewed set, stores `released_keys`, and promotion re-holds anything outside it; the marker scan and the OOXML scan keep running for released files (D2d, 5a, 5f). |
| safety-security#3 | fixed | Same root as code-correctness#3; the card uses a valid scan or the substring counts of the RED markers only (never `/AA`) and marks the result provisional (5a `annotate_entries`, 8.2). |
| safety-security#4 | fixed | `.doc` / `.xls` releases promote the converted PDF only (D2d). |
| safety-security#5 | fixed | Verified (notifications.py:75-76). Notify first, record in `it_alerted` only when `notify_role` (now returning a count) wrote a bell; dedupe keyed on `tier:code` so escalations re-alert; `files_unalerted` drives the "no IT Admin" copy (5a step 10, 7, 8). |
| safety-security#6 | fixed | Verified (rfp_email_files.py:176-210, rfp_sanitize.py:89-104). `SUSPICIOUS_EXTENSIONS` plus `rtf` and `ps` magics make `not_pdf` RED (D1g, E2, R5). |
| safety-security#7 | fixed | `clean_name` now NFC-normalizes and removes categories Cc, Cf, Co, Cs, Zl, Zp; alert names are quoted (3.1, 7). |
| safety-security#8 | fixed | Verified (graph_email.py:254-262, rfp_test.py:353-357). `session_frozen` skips follow-ups, automatic re-checks and bells for records of an ended session (DF5, 5a, 5c, 7). |
| safety-security#9 | fixed | The caution modal lists up to 20 held names (fetched, or from the card's own entries) and the harvest card's "Open link" anchors get the same step through `RfpCautionLink` (8.1, 8.2). |
| safety-security#10 | fixed | Verified (rfp_ingest.py:1880-1891, 2134; rfp_sandbox_runner.py:1510-1511). First `invalid_output` is rerunnable; a repeat after a reset (`prior_fail_code`) is RED (D1b, X1a, X1b). |
| safety-security#11 | fixed | Hostile-looking fixtures travel only through a Dropbox share link the user creates; the Defender risk is stated at the step (10.3 step 4). |
| safety-security#12 | fixed | Release classifies live, binds to `released_status` and `released_keys`, and a change re-holds and re-alerts (P12); other projects get a pass and the release note on every row. A separate bell to other projects' Estimating Admins is not added: the note on the file is the record, and before the hand-off such a landing is an ordinary promotion. |
| safety-security#13 | fixed | `ai_rate_limit` on re-check and release, `_uuid_or_404` on both path ids, and the release order is CAS, pass request, then a logged audit (5f, 6). |
| safety-security#14 | fixed | Partly. P1 / P2 sentences now say our stored copy changed and IT was told; the D1c rationale names the UNC / NTLM risk; the release intro names the estimators and vendor RFQs. P1 / P2 stay RED with the tier-level "do not open" copy because D1 (user-locked) puts bytes changed after verification in RED. |
| safety-security#15 | fixed | Verified (pdf_split.py:40, rfp_split.py:614). Safety argument (4) restated as memory safe but not DoS safe; `pdf_split` drops the exception text (package B). |
| safety-security#16 | fixed | Verified (bid_splitter.py:250-290, :66-72). Hand uploads into `source = 'rfp'` jobs answer 409 (5c, package C). |
| safety-security#17 | fixed | Partly. `promote_split_file(release_note=)` carries the release note onto segments, the source set and the intact row (5c). A FilesPanel "released by IT" badge is not added: the note is shown on the file already and keeps this slice smaller. |
| completeness#1 | fixed | Same as code-correctness#1 (D2d, 5a, 5f, package A tests). |
| completeness#2 | fixed | Same as code-correctness#8; `recheck_available`, `recheck_auto` and `files_recheckable` on the summary and list; a test covers `recheck_auto_at` null outside `scheduled` (6, 8, package A tests). |
| completeness#3 | fixed | Same as code-correctness#6, with the package A test named in the finding. |
| completeness#4 | fixed | Same as code-correctness#7, assigned to package A with a test. |
| completeness#5 | fixed | Verified (rfp_create.py:1246-1276). Notes by provenance: released, rescued (held on an earlier pass), or a neutral "added after the package was sent" that is never false; the bell counts each kind (D4b, 5a step 5.8, 7). |
| completeness#6 | fixed | Same as code-correctness#2. |
| completeness#7 | fixed | Verified (rfp_email_harvest.py:404-413, 363-380; rfp_zip.py:171-188). Zip entries carry `reject_code` (E15, E16), skipped members are listed (Z1 to Z5), the dedupe keys on entry keys so they can alert, and the exhaustiveness test walks rfp_zip's reasons (D1h, 3.2 rule 5, 4.4). |
| completeness#8 | fixed | Partly. The harvest card shows a Callout and the per-file sentence inline, with a caution on its links. A pre-creation IT alert needs a new trigger and is recorded as open question 4. |
| completeness#9 | fixed | Package F adds `tests/test_rfp_file_flow.py` over the real modules as a gate before the live test (9, 10). |
| completeness#10 | fixed | The live plan now runs flags off, both deadline paths (5 minute setting, occupied slot, SQL-unowned run), the post-hand-off rescue and release, an engineer view, the E2 fixtures, a simulated expiry and the recheck-needed bell (10.3 steps 6 to 14). |
| completeness#11 | fixed | D3a is open question 3 with its upper bound. |
| completeness#12 | fixed | Partly. `config.py` is a sequential Step 0 before the parallel packages; B shed the harvesters (to E) and the splitter router guard (to C). B is not split in two, to keep five parallel packages as the brief requires. |
| completeness#13 | fixed | (a) IT Admin viewers get their own hint; (b) `notify_role` returns a count and `files_unalerted` drives "No IT Admin could be notified"; (c) the checking help covers the splitter; (d) not_usable help says to ask the sender for a checkable copy; (e) `rtf` and `ps` join the RED magics (D1g). |

Totals: 52 findings; 52 fixed (6 of them partly: safety-security#1, #12,
#14, #17 and completeness#8, #12, each with the part not taken and why),
0 rejected outright.
