# PM RFI sync from the GC's Procore

Migration 0156. Code: `app/services/pm_rfi_sync.py` (sync, queue, scheduler,
email scan), `app/services/procore_client.py` (read-only RFI paths),
`app/routers/pm_rfi_sync.py` (link + sync routes), `app/routers/pm_field.py`
(the RFI log, read-only guards). Tests: `tests/test_pm_rfi_sync.py` with
fixtures in `tests/fixtures_pm_rfi/` (trimmed real Procore captures).

## 1. What it does

A PM project can be linked to the GC's project management app. When the
link is Procore, every RFI on the GC's Procore project (all trades, not just
G3's) is imported into the project's RFI log and kept current:

- once a day at 06:30 America/Los_Angeles (DST aware),
- when a writer presses "Sync now",
- when a filed PM email carries a Procore RFI link (or is classified as an
  RFI email, see section 5).

Procore is strictly READ ONLY. The client only sends GET requests on its RFI
allowlist (`RFI_ALLOWED_PATHS`: the RFI list, one RFI's detail, the account's
companies and one project, used to resolve a company id), plus the two login
form posts the harvest already makes. There is no code path that writes to
Procore.

We use the logged-in Procore web session the RFP harvest already uses
(`PROCORE_LOGIN_EMAIL` / `PROCORE_LOGIN_PASSWORD`). Procore's official API
was ruled out because every GC admin would have to install an app. On
app.procore.com the sync shares the harvest's `rfp_harvest_sessions` row
(provider `procore`): one cookie jar, one failure counter, one lock, and the
same process-wide pace (`PROCORE_MIN_REQUEST_INTERVAL_SECONDS` x U(1, 2)).

## 2. Data model (0156)

- `pm_external_links`: one row per project. `platform` procore | other |
  none, `host` (app.procore.com or us02.procore.com), `external_company_id`,
  `external_project_id`, `external_url`, `sync_enabled`, `linked_via`
  manual | email, `last_synced_at`, `last_sync_status` ok | failed,
  `last_error`, `backfilled_at`, `backfill_skipped_ids` (Procore ids the
  backfill was refused; imported later without a "new" notice). A partial
  unique index keeps two BDR projects off one Procore project.
- `rfis`: `rfi_number` is now nullable; `source` internal | procore (an
  internal row must carry a number). New columns: `external_id`,
  `external_number` (TEXT, "Const-198", "Const-189.1"), `external_revision`,
  `external_status` (Procore's translated status), `external_url`,
  `responsible_contractor`, `received_from`, `initiated_at`,
  `ball_in_court`, `external_due_date`, `external_updated_at`,
  `external_synced_at`, `external_raw` (a compact record, no bodies),
  `external_autolink_blocked`, `source_email_id`. Unique indexes:
  (project_id, rfi_number) where rfi_number is not null (replaces the old
  constraint), (project_id, external_id), and (project_id, external_number,
  external_revision) where source = 'procore'.
- `rfi_attachments.external_file_id`: the Procore file id, unique per RFI.
- `pm_rfi_sync_runs`: the run queue (trigger daily | manual | email,
  `scheduled_for` for the daily slot, `target_external_rfi_id`,
  `source_email_id`, status queued | running | done | failed, lease,
  attempts, `next_attempt_at`, `stats`, `error`). The unique index on
  (project_id, trigger, scheduled_for) where trigger = 'daily' is the slot
  ledger; one email run per email is unique too.
- `pm_rfi_email_scan_state`: the email scan's checkpoints and lease.

## 3. A sync run

1. Resolve the company id if the link has none (project home redirect, else
   the account's companies), and save it.
2. GET `/rest/v1.0/projects/{id}/rfis` with `Procore-Company-Id` (Procore
   returns every row when `per_page` is omitted).
3. For each row: skip drafts; skip rows whose local copy is closed and
   complete (closed rows are never re-fetched once complete, and never
   reopened); skip rows whose `updated_at` matches `external_updated_at`. Everything else gets its
   detail fetched (list rows lack answers and attachments). At most
   `PM_RFI_SYNC_MAX_DETAILS_PER_RUN` details per run; the rest wait.
4. Each detail becomes or updates a row keyed by the Procore id. Each
   revision (Const-97 r0, r1) is its own row. "Const-189.1" is a separate
   RFI, not a revision.
5. Attachments download once into the project's Documents (`pm_documents`,
   category rfi, `{project}/pm/rfi/...`) and attach with kind question or
   answer. The signed storage.procore.com links download without cookies.
   A file over `PM_RFI_SYNC_ATTACHMENT_MAX_BYTES` (150 MB) is skipped and
   recorded in the run's `stats.skipped_files`. Files are stored with the
   house safe content type (`routers/files._safe_content_type`).
6. A row is written with `external_updated_at` null and gets Procore's
   `updated_at` only after its attachments landed ("complete"). A file that
   fails for now (storage 5xx, network, upload error; reason `retry` in
   `stats.skipped_files`) leaves the row incomplete, so the next run fetches
   its detail again, closed or not, and picks the file up.
7. An insert that collides on (external_number, external_revision) with a
   row no longer in Procore's list (deleted there, number re-used) removes
   the stale row and takes its place (`stats.retired`).

Imported row content: subject; question = the sanitized rich text of every
question (`sanitize_rich_text`); answer = the official answers (all answers
when none is official), plain text, oldest first; status = closed for any
closed* Procore status, answered for an open RFI with an answer, else open;
`due_at` = Procore due date, `sent_at` = date initiated (Pacific).

Imported rows are read only: PATCH, close, send, mark-sent and delete answer
409 "This RFI is imported from Procore and is read-only."

## 4. Linking G3's own RFI

When G3's RFI (an internal row) is in the GC's Procore, it is LINKED rather
than duplicated. The internal row gets the external_* fields; later syncs
update its Procore status, answer and answer attachments, never its question,
and mark it sent via Procore if it was not marked sent.

Auto-link happens only when confident, among the project's internal rows with
no `external_id` (and not blocked by an Unlink):

- a QUESTION attachment on the Procore RFI is named like `RFI 040 - ...`
  (`^RFI[ #-]*0*<n>\b`, case-insensitive), naming exactly one internal RFI.
  Answer files are ignored (GC responses are named after the GC's own
  number), and so is a number equal to the Procore RFI's own number; or
- the normalized subject equals exactly one internal RFI's subject.

Anything ambiguous imports as its own row. Writers can link by hand:
`POST /rfis/{imported_id}/link {internal_rfi_id}` moves the external fields
and attachments onto the internal row and deletes the imported row.
`POST /rfis/{internal_id}/unlink` clears the external fields and the
Procore-imported attachments and blocks auto-link for that row; the next
sync re-imports the Procore RFI as its own read-only row (its files are
reused from Documents, matched by the `[procore-file:<id>]` note marker).

## 5. Triggers

- Daily: the scheduler queues one run per linked, sync-enabled project at
  06:30 Pacific (`PM_RFI_SYNC_DAILY_TIME`). A worker that was down still
  queues it inside `PM_RFI_SYNC_CATCHUP_HOURS` (4); after that the day is
  skipped. The daily unique index means the two uvicorn workers never queue
  it twice.
- Manual: `POST /pm/projects/{id}/rfis/sync`, or saving a new Procore link.
- Email: every `PM_RFI_EMAIL_SCAN_INTERVAL_SECONDS` (300) a scan reads
  `ingested_emails` updated since its checkpoint (first scan: the last
  `PM_RFI_EMAIL_SCAN_LOOKBACK_HOURS`, 72), inbound, filed to a PM project,
  whose body has a Procore RFI link
  (`https://{app|us02}.procore.com/{project}/project/rfi/show/{rfi}`, Outlook
  safelinks unwrapped). Bid-phase Procore emails (route_to_bid_sheet,
  planroom) never match. One link to the linked project queues a targeted
  run of that RFI; several queue a full run. If the project is not linked
  yet, and the email is plainly Procore's (sender on procore.com or
  procoretech.com, or a signed storage.procore.com link whose projectId is
  the RFI link's project), it is linked automatically (host and project id from the link, company
  id from a `storage.procore.com ...companyId=` link in the same email, else
  resolved by the run) unless another BDR project is already linked to that
  Procore project. A project linked to Other or None is left alone.
  When the `email_classifications` table exists (0154), a `done` row whose
  effective labels (user labels, else AI labels) include category `rfis` on
  an email of a linked PM project queues a full run too. The code tolerates
  the table not existing. The scan never edits `email_ingest.py`.

A targeted run after the backfill fetches only that RFI's detail. The first
run of a project is always a full backfill, and it counts as done only when
no detail waited on the per-run cap. "Sync now" never joins a targeted
email run. Saving a link to a different Procore project queues a fresh run
even while an old run works; the old run drops what it imported when it
sees the link moved.

## 6. Queue, leases, failures

Every worker runs the loop (`polling_loop`, every `PM_RFI_SYNC_POLL_SECONDS`)
started from `app/main.py` when `PM_ENABLED` and `PM_RFI_SYNC_ENABLED`. A run
is claimed by compare-and-set on its status and lease
(`PM_RFI_SYNC_LEASE_SECONDS`, renewed while it works); a project with a live
run is skipped (and two runs of one project claimed in the same instant
resolve to the older one). A running run whose lease expired is reclaimed
at most until `PM_RFI_SYNC_MAX_ATTEMPTS`, then failed. Queued daily and
email runs fail without work once sync is turned off. A transient Procore failure (network, 5xx, 429, a login
attempted too recently) is retried after `PM_RFI_SYNC_RETRY_SECONDS` up to
`PM_RFI_SYNC_MAX_ATTEMPTS`; a login lock, a 403/404 on the project, missing
credentials or an unresolvable company fail the run with a stored message,
and the link shows `last_sync_status = failed` and `last_error`. A login
lock also rings the harvest's IT Admin bell (shared with the RFP harvest).
An RFI read that still answers "session gone" right after a fresh login
fails the run without another login. The new, answered and closed RFIs a
failed attempt already saved ride along to the retry in the run's `stats`;
a run that fails for good still sends the summary of what it saved.
A dropped database or storage connection (an httpx transport error: a
Wi-Fi change, a laptop asleep) is retried like a transient Procore
failure. A run whose worker died (lease expired) is reclaimed; past the
attempt cap it is failed and the link shows `last_sync_status = failed`.

## 7. Notifications

To Estimating Engineer Labor and Estimating Admin, in-app plus the branded
notification email (`notify_role` mirror), one summary per project per run:
new RFIs, newly answered, newly closed (type `pm_rfi.sync_summary`, deep link
`/pm/projects/{id}/rfis`). The first sync (backfill) sends no per-RFI
notices, only one in-app "Linked to the GC's Procore project: imported N
RFIs" (type `pm_rfi.linked`, no email). N is counted from the
project's rows, so a backfill that finished on a retry still names every
RFI it imported. Both types are in
`PM_NOTIFICATION_TYPES`; the frontend's copy in `bdr_fe/lib/features.ts`
must list them too.

## 8. API

All under `/pm/projects/{project_id}`, PM read / write roles, 404 unless
`PM_ENABLED` and `PM_RFI_SYNC_ENABLED` (`GET /features` -> `pm_rfi_sync`).

- `GET /pm-link` -> PmExternalLink (`platform` procore | other | none | null,
  `host`, `external_company_id`, `external_project_id`, `external_url`,
  `sync_enabled`, `linked_via`, `last_synced_at`, `last_sync_status`,
  `last_error`, `backfilled_at`, `running`).
- `PUT /pm-link {platform, url?, sync_enabled?}` -> PmExternalLink. For
  procore, `url` is any Procore project URL (project home, an RFI link, the
  RFIs tool page `.../webclients/host/companies/{c}/projects/{p}/tools/rfis`).
  422 when unparseable, 409 when another BDR project has that Procore
  project. The company id comes from the URL when present, else one or two
  GETs through the session. A new Procore link queues its backfill.
- `POST /rfis/sync` -> `{run_id, status}`; 409 when not linked to Procore;
  joins a queued or running run instead of adding one.
- `GET /rfis` (pm_field): each row adds `source`, the external_* fields and
  `read_only`. G3's rows first by number, then Procore rows by number
  (natural sort) and revision.
- `POST /rfis/{rfi_id}/link {internal_rfi_id}`, `POST /rfis/{rfi_id}/unlink`
  (section 4). Audited as `rfi.link` / `rfi.unlink`; link changes as
  `pm_link.update`, Sync now as `rfi.sync`.

## 9. Flags and env vars

| Variable | Default | |
|---|---|---|
| `PM_RFI_SYNC_ENABLED` | false | master switch (also needs `PM_ENABLED`) |
| `PM_RFI_SYNC_DAILY_TIME` | 06:30 | Pacific HH:MM |
| `PM_RFI_SYNC_CATCHUP_HOURS` | 4 | missed slot still runs inside this |
| `PM_RFI_SYNC_POLL_SECONDS` | 30 | worker tick |
| `PM_RFI_SYNC_LEASE_SECONDS` | 900 | run lease (renewed) |
| `PM_RFI_SYNC_MAX_ATTEMPTS` | 3 | transient retries |
| `PM_RFI_SYNC_RETRY_SECONDS` | 600 | wait between retries |
| `PM_RFI_SYNC_ATTACHMENT_MAX_BYTES` | 157286400 | 150 MB per file |
| `PM_RFI_SYNC_MAX_DETAILS_PER_RUN` | 1000 | detail fetches per run |
| `PM_RFI_EMAIL_SCAN_ENABLED` | true | email trigger |
| `PM_RFI_EMAIL_SCAN_INTERVAL_SECONDS` | 300 | |
| `PM_RFI_EMAIL_SCAN_LOOKBACK_HOURS` | 72 | first scan window |

Plus the shared Procore settings: `PROCORE_LOGIN_EMAIL`,
`PROCORE_LOGIN_PASSWORD`, `PROCORE_MIN_REQUEST_INTERVAL_SECONDS`,
`PROCORE_LOGIN_MIN_INTERVAL_SECONDS`, `PROCORE_LOGIN_MAX_FAILURES`,
`PROCORE_LOGIN_LOCK_SECONDS`, `PROCORE_REQUEST_TIMEOUT_SECONDS`.

## 10. Limits and known gaps

- us02.procore.com is UNTESTED live. A us02 link keeps its cookies in its
  own session row (provider `procore:us02.procore.com`) and logs a warning
  each time, but its logins share the `procore` row's lock, failure count
  and login spacing (the same account posts to the same login.procore.com). us02 ids run to 15 digits; the RFI paths accept up to 20.
- Pace: about 3 s per request on average. Measured on dev (2026-10-19,
  Lake Mead, 209 RFIs): the backfill made 1 list + 193 detail GETs and 362
  file downloads in 33 minutes on its final attempt (396 files, 570 MB in
  all, largest 23 MB). Later runs fetch only changed rows: the next manual
  and daily runs made one list GET each and fetched no details.
- An attachment is held in memory once while it is uploaded (up to 150 MB).
- Re-pointing a link to a different Procore project (or to Other / None)
  deletes the rows imported from the old project and unlinks G3's linked
  rows (their Procore files stay in Documents); a Procore link starts a new
  backfill.
- The email scan reads `ingested_emails.body_text` (Graph text form, which
  keeps link URLs). A body truncated before its links will not trigger.

## 11. How to link a project and test on dev

The dev database is BDR (`bpidntbyvoooqvaispup`, what `bdr_be/.env` points
to); `PM_RFI_SYNC_ENABLED=true` is set there. Never on BDR_Prod or Staging
without approval.

From the UI: open the PM project's RFIs page, choose Procore in the "GC
project management app" card, paste the GC project's URL, Save. The backfill
queues at once; the running dev server's loop runs it.

From a script (dev server stopped, or let the server's loop claim the run):

```bash
cd bdr_be && PYTHONPATH=. uv run python - <<'PY'
from app.core.config import get_settings
from app.core.supabase_client import get_supabase
from app.services import pm_rfi_sync as s
sb, st = get_supabase(), get_settings()
PROJECT = "<bdr project uuid with a pm_stage>"
s.save_link(sb, PROJECT, platform="procore", sync_enabled=True, actor_id=None,
            url="https://app.procore.com/webclients/host/companies/6333/projects/3167322/tools/rfis")
s.enqueue_run(sb, PROJECT, "manual")
run = s.claim_next(sb, st, s._now())
print(s.process_run(sb, run, st))
print(sb.table("pm_rfi_sync_runs").select("status, error, stats").eq("id", run["id"]).execute().data)
PY
```

Check `rfis` (source = 'procore'), `rfi_attachments` (external_file_id),
`pm_documents` (category rfi), `pm_external_links.backfilled_at`, and the
bell. Run the script again: `details_fetched` should be 0.

## 12. Live dev test (2026-10-19)

Project "ZZ TEST Lake Mead RFI Sync" (number ZZ-RFI-SYNC-01, id
`b5f1c246-bb0c-4c2a-8723-393ed933b2fc`) on dev BDR is linked to Procore
project 3167322 (company 6333, resolved from the project home redirect with
one GET). It holds the 209 imported RFIs, G3 RFI 40 linked to Const-198,
and "ZZ TEST" ingested emails used for the email trigger. "ZZ TEST RFI Sync
Conflict" (ZZ-RFI-SYNC-02) checked that an RFI email cannot auto-link a
second project to an already linked Procore project. The stored Procore
session was still valid, so no login was made.
