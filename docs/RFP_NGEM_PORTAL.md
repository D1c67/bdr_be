# RFP Ingestion: NGEM Portal Invitations

Design record for the NGEM slice of RFP Ingestion. NGEM (the Nevada
Government eMarketplace, an Ionwave / Euna Procurement supplier portal at
`supplier.ionwave.net`) does not send usable invitation mail, so the
application logs into the company's supplier account on a schedule, reads
the invitations addressed to us, decides with the existing matcher whether
each one is already a project, and for the new ones pulls the notes and the
bid attachments into an Ingestion Sandbox run. It is its own feature next
to the email pipeline: its own tables, its own tab, its own scheduler. It
shares the matcher (`rfp_match.py`), the harvest record (`rfp_harvests`),
the session store (`rfp_harvest_sessions`), the sandbox and the queue.

Status: design v1 plus the review round of section 12, 2026-09-16, written
from a live capture of the portal
with the company's account (login, the invitations grid over two pages, one
bid's Event Details and Attachments pages, one file download; all over plain
`httpx`, no browser). Dev database only. Nothing here creates projects,
responds to a bid or writes anything to the portal.

Naming, used everywhere: setting prefix `rfp_ngem_` (env `RFP_NGEM_`) plus
`ngem_` (env `NGEM_`), pure client `app/services/ngem_client.py`, service
`app/services/rfp_portal_ingest.py` (schedule, scan, match, human actions,
harvest job), migration `0126_rfp_portal_invitations.sql`, tables
`rfp_portal_invitations` and `rfp_portal_runs`, queue job types
`rfp_portal_scan` and `rfp_portal_harvest`, router `app/routers/
rfp_portal.py` at `/rfp-portal`, sandbox run source kind `rfp_portal`,
harvest method `ngem`, bell types `rfp_ngem.login_failed`,
`rfp_ngem.new_invitations`, `rfp_ngem.invitation_changed`,
`rfp_ngem.scan_failed`, FE tab "NGEM" on `/rfp-emails`, FE namespace
`rfpPortal`. The `portal` column is `ngem`
everywhere today; a second portal is a second client behind the same
tables.

---

## 1. Decisions locked in (2026-09-15, with the user)

| Topic | Decision |
|---|---|
| Shape | Own records and own surface, not `rfp_emails` rows. One `rfp_portal_invitations` row per (portal, agency, bid number). A "NGEM" tab on `/rfp-emails` with its own list, detail and actions. |
| Schedule | Twice a day, every day, at the Pacific wall-clock times in `RFP_NGEM_SCHEDULE_TIMES` (default `06:30,12:00`). A run that was missed (the app was down) still runs within `RFP_NGEM_CATCHUP_HOURS` (4), then that slot is skipped. Plus a "Run now" button. |
| Scope of the list | Only the "My Invitations" grid of the Available Bids tab. Never "Other Bid Opportunities", never the other tabs. Rows whose close date is already past at scan time are not recorded. |
| Dedup key | Bid numbers are agency-scoped and free-form ("1790", "27300209", "IFB 112-27 Fire Station 95 Interior Renovation Addendum 1"), and the portal appends "Addendum N" to the number when addenda exist. Key = `(portal, agency, bid_number)` where `bid_number` is the raw number with a trailing `Addendum N` stripped (case-insensitive, `\s+Addendum\s+(\d+)\s*$`); the addendum number is kept in `addendum_no`. |
| Match first, harvest after | Title (as the project name) and close date (as the bid due date) go through the same matcher the email pipeline uses, with the same weights, thresholds and LLM verdict. No LLM extract: the facts are structured. Notes and attachments are pulled only for invitations that are not already projects. |
| What a match means | No GC is involved. A confident match (score plus a confident "same" verdict) resolves automatically to `exists`: nothing on the project changes, the invitation records the project, and the project shows that it also came from NGEM. `RFP_NGEM_AUTO_RESOLVE_ENABLED` (default true) is the switch; off means confident matches wait for a click like the email side. Uncertain (middle band, two candidates too close, unusable model output) waits at `review_match` in the NGEM tab. No candidate: `harvest`. |
| The model can be off | The scan never needs the model. An invitation whose match needs a verdict waits at `match` without spending an attempt while the model is unavailable (the email pipeline's box-off pattern) and is picked up on a later sweep. Invitations with nothing above the review threshold need no verdict and go to harvest right away. The harvest never needs the model. |
| Changes to a known bid | Every scan re-sees every open invitation. A changed title, close date or addendum number is written to the row and appended to `change_log`, and every Estimating Admin gets one bell per invitation per change (deduped while an unread one for that invitation exists). Re-harvesting is manual ("Harvest again"); nothing is downloaded twice on its own. |
| Attachments | Every file on the bid's Attachments page, through the per-file Download links a person clicks, one at a time, into one sandbox run per harvest (`source_kind = rfp_portal`). PDFs are verified; Word and Excel files are converted and verified (0125); anything else is recorded `rejected/not_pdf`. "Download All" (a zip postback) is never used. |
| Ignore | A person can mark an invitation `ignored` (reason optional) at any non-terminal status, and un-ignore it. Nothing is filtered automatically. |
| Access | Plain HTTP with the company's shared supplier account (`NGEM_LOGIN_USERNAME` / `NGEM_LOGIN_PASSWORD`, no MFA, humans use it during the day, concurrent sessions are fine). One persisted session (`rfp_harvest_sessions`, provider `ngem`) shared by both workers. Human pace. GET-only apart from the login POST and the two grid pagers. Opening a bid flips its agency-visible Response Status to "Viewed", as a person would; the scan itself flips nothing. |
| Who sees it | `RFP_REVIEW_ROLES` see the tab and may Run now, resolve reviews, ignore, harvest again. Match scores are redacted for engineers the way the email drawer does it. The sandbox run link needs the sandbox gate. The settings block is for the manage roles. |

---

## 2. Pipeline

```
scheduler (every 60 s, both workers)        Run now (route)
   | claim rfp_portal_runs(portal, slot)        | insert trigger=manual
   v                                            v
 job rfp_portal_scan(run_id)   log in if needed, page the My Invitations grid,
                               upsert invitations, detect changes, close the run
   |
   v  invitation status
 match         sweep step: candidates from the shared reference bundle, the
   |           LLM verdict when something clears the review threshold, route
   |             confident + auto-resolve on   -> exists      (terminal)
   |             confident, switch off / middle band / too close / unusable
   |                                          -> review_match (human)
   |             nothing above the threshold  -> harvest
   v
 harvest       sweep step: make sure an rfp_portal_harvest job exists
   |
   v  job rfp_portal_harvest(invitation_id)
 done          facts on the rfp_harvests row first, then the files into a
               sandbox run; the invitation ends at done with harvest_id

 review_match  (human: Same as project X -> exists | Not a match -> harvest,
                project excluded)
 exists        (terminal; Reopen -> match with the project excluded)
 done          (terminal; Harvest again = a forced job)
 ignored       (terminal; Un-ignore -> match, attempts reset)
```

Status vocabulary: pending `match`, `harvest`; human `review_match`;
terminal `exists`, `done`, `ignored`. There is no `failed` status: a harvest
that fails permanently ends the invitation at `done` with the failure on the
harvest row (as the email side does), and a match step that exhausts its
attempts parks the invitation at `review_match` with `flag_reason =
match_failed` so a person sees it.

### 2.1 Scheduler (`rfp_portal_ingest.poll_once`, wired in `main.py` lifespan)

Runs every `RFP_NGEM_POLL_SECONDS` (60) in every worker when `rfp_ngem_active`
is true (`rfp_ingest_enabled and rfp_ngem_enabled and ngem_configured and
llm_queue_enabled`). Each tick:

1. Compute the slots: for every `HH:MM` in `RFP_NGEM_SCHEDULE_TIMES`, the
   Pacific-local datetime of that time today, as an aware instant. A slot is
   due when `slot <= now < slot + catchup_hours`.
2. For each due slot, claim it by inserting `rfp_portal_runs (portal,
   scheduled_for = slot, trigger = 'scheduled', status = 'queued')`; the unique
   index on `(portal, scheduled_for)` makes the second worker's insert a
   23505, which means "already claimed" (idempotent across workers and
   restarts, the due-digest ledger pattern). A losing insert does nothing.
3. A claimed run enqueues `rfp_portal_scan` (priority
   `RFP_NGEM_SCAN_QUEUE_PRIORITY`, 140, ahead of the harvests at 150 so the
   noon scan never queues behind a morning's harvest backlog) with
   `target_id = run.id`. An enqueue failure parks the run (`queued`,
   `next_attempt_at` one poll interval out, the sentence in `last_error`)
   instead of failing it: one transient PostgREST error must not cost the
   day's slot. Every tick re-dispatches each `queued` run that has no scan
   job once its wait has passed, or at once when it has none (a crash
   between the claim and the enqueue, an insert that answered no row);
   `RFP_NGEM_SCAN_TIMEOUT_SECONDS` stays the cap that fails it.
4. The invitation sweep (2.3) runs in the same tick under its own lease
   (`rfp_portal_sweep`, the email sweep's `_acquire_lease` / `_renew_lease`
   helpers reused as public names).

At most one run per portal may be `queued` or `running` (partial unique
index); a scheduled slot that finds an active run waits for the next tick
(the slot stays due until its catch-up window closes). A run whose job was
lost (lease expired, queue marked it terminal) is marked `failed` by the
sweep when it has been `running` for longer than `RFP_NGEM_SCAN_TIMEOUT_SECONDS`
(1800) with no active job. Every run that ends `failed`, whichever path
failed it, logs the sentence and rings `rfp_ngem.scan_failed` to every IT
Admin (deduped while an unread one exists; the message carries the run's
`last_error`, capped). The scheduler logs at INFO when a slot is claimed,
when a run is dispatched (job id and priority), when a run completes (the
counts) and when one fails (the sentence).

### 2.2 The scan job (`rfp_portal_ingest.execute_scan(run_id)`)

Registered in `llm_queue._spec` as `JOB_PORTAL_SCAN = "rfp_portal_scan"`,
non-LLM, model label `ngem`, claimed in the third pass with the harvest
capacity (it shares `rfp_harvest_concurrency` with `rfp_harvest` and
`rfp_portal_harvest`: one portal request stream per worker at a time).

1. Load the run; must be `queued` or `running` (the queue's ladder
   re-runs the same job); a `failed` run is refused, a `complete` one
   returns at once (a retry after a late failure is idempotent). CAS
   `queued|running -> running` with `started_at` and a fresh per-attempt
   `claimed_by` token; every later write of the attempt (park, the
   transient sentence, complete, fail) is fenced on `(id, status,
   claimed_by)`, so a zombie attempt whose lease expired can never write
   over the attempt that owns the run now.
2. `ngem_client.NgemSession(config, store).fetch_invitations(max_pages)`:
   the entry URL (a stored session answers 200 with the grid; a 302 to
   `/Login.aspx` or `/VendorLogin.aspx` means log in, section 3.2), then
   the pager postbacks for pages 2..N, capped at `RFP_NGEM_MAX_LIST_PAGES`
   (20). Every page is parsed pure (3.3). The lease is renewed before every
   request.
3. For every parsed row, in grid order, keyed `(agency_key, bid_number)`
   with the same agency fallback the stale-link re-find uses (an empty
   agency is stored as "Unknown agency" and keys as `unknown-agency`):
   - a second grid row with a key already seen in this scan (agencies that
     differ only in punctuation or case, a bid listed twice): the first
     row's values stand, the duplicate is logged at INFO;
   - close date in the past (or unparseable): counted `skipped_closed`, not
     stored, but counted as seen (a closed bid the grid still lists is not
     missing from it);
   - `(portal, agency, bid_number)` new: insert with `status = match`,
     `first_seen_run_id`, `last_seen_run_id`, `seen_count = 1`; counted `new`;
   - known: update `last_seen_at`, `last_seen_run_id`, `seen_count`,
     `missing_since = null`, `response_status`, `bid_status`, `time_left`,
     `view_url`; compare `title`, `close_at`, `addendum_no` and
     `bid_number_raw`: a difference writes the new values, appends
     `{at, field, old, new, run_id}` entries to `change_log` (capped at 50,
     oldest dropped) and, unless the row is `ignored`, rings
     `rfp_ngem.invitation_changed` to every Estimating Admin (one bell per
     invitation while no unread one for that invitation exists; the message
     names the agency, the bid number, the title and what changed); counted
     `changed`.
   - Titles are stored as text, capped; an empty title is stored EMPTY
     (never an app-authored placeholder, which the matcher would score and
     the bell would repeat: the match step routes it to harvest as
     `no_project_name` and the frontend renders its own placeholder); the
     view URL is the grid's link (session-bound token, refreshed on every
     scan).
4. Rows known to the table but absent from this scan (and not `ignored`)
   get `missing_since = now` if null. Nothing else happens to them.
5. Close the run: `complete` with `finished_at`, `pages_scanned`,
   `invitations_seen`, `invitations_new`, `invitations_changed`,
   `invitations_skipped_closed`. Then, outside the failure handling (a bell
   failure is logged and never sends a complete run down the retry ladder),
   one bell `rfp_ngem.new_invitations` per review-role user when
   `invitations_new > 0` ("N new NGEM invitations", metadata `{run_id,
   count}`), deduped per `(type, user, run_id)`: a user who still holds an
   unread bell for the same run is not rung again, a user who read theirs
   is, and nobody's unread bell silences anyone else's. The change bell is
   deduped the same way per `(type, user, invitation_id)`. Bell text is
   capped (each portal value 80 characters, the title 120, the whole message
   600).

Failure mapping: `NgemLoginLocked` / `NgemUnavailable` (locked, credentials
missing, an interstitial): the run goes back to `queued` with `last_error`
and `next_attempt_at` pushed by the lock remainder or the poll interval;
the job returns normally (no attempt spent). `NgemTransient`
(`httpx.TransportError`, 5xx, 429, the portal's `/Error.aspx` answer): the
queue's retry ladder, run stays `running` with `last_error`. `NgemForbidden`
and a page whose shape is not the invitations grid (`NgemParseError`):
`failed` with the sentence; a person sees it in the settings block and runs
again. The queue's terminal failure calls `mark_scan_from_queue` (CAS-fenced).

### 2.3 The invitation sweep (`rfp_portal_ingest.sweep`)

Rows at `match` or `harvest` with `next_attempt_at` null or past, oldest
first, up to `RFP_NGEM_SWEEP_BATCH` (50) per tick, queried per portal of the
`PORTALS` registry (`portal = ...` so the `(portal, status, next_attempt_at)`
index carries it), each step CAS-fenced on `(id, status)` exactly as the
email sweep fences on `(id, status)`.

`match` (mirrors `rfp_email_ingest._step_match` without the GC and sender
parts; the reference bundle is shared: `rfp_email_ingest.reference_bundle`
becomes the public name of `_reference_bundle`, with a small `_SweepState`
of its own per tick):

1. facts = `rfp_match.ExtractedFacts(project_name = title, gc_name = None,
   bid_due_at = close_at, has_time = True, bid_notes = None, reasoning = "")`.
   A title that normalizes to nothing goes straight to `harvest` with
   `flag_reason = no_project_name` (it cannot be scored; a person can still
   ignore it).
2. Candidates: the bundle's projects inside the candidate window, minus
   `excluded_project_ids`, through `rfp_match.rank_candidates` and
   `model_candidates`.
3. When something clears the review threshold: the LLM gate. The health
   snapshot (`rfp_email_ingest.model_unavailable(snapshot, FEATURE_MATCH)`)
   or a connection error / timeout on the live call parks the row with
   `next_attempt_at` pushed by the same wait the email step uses
   (`_wait_for_model` logic, mirrored on this table), attempts untouched.
   Other call failures spend an attempt through the same ladder
   (`attempt_decision`, `backoff_seconds`); at the cap the row goes to
   `review_match` with `flag_reason = match_failed` and `last_error`.
   Unusable output twice: `review_match`, `flag_reason = match_llm_unusable`.
4. `rfp_match.route(candidates, has_name=True, has_date=close_at is not
   None, gc_resolved=True, gc_on_project=True, sender_verified=True,
   auto_merge=settings.rfp_ngem_auto_resolve_enabled, settings)`. The route
   statuses map: `duplicate` and `merged` -> `exists`; `review_match` ->
   `review_match`; `done` -> `harvest`. The rebid lookup runs for
   `review_match` and `harvest` exactly as the email step does and fills
   `possible_rebid_project_id` / `possible_rebid_score`.
5. Write `match_candidates`, `match_project_id`, `match_score`,
   `match_llm_model`, `match_llm_prompt_version`, `match_weights`
   (`settings_snapshot` plus `auto_resolve_enabled`, the switch this step
   routed by; the snapshot's own `auto_merge_enabled` is the email side's),
   `matched_at`, `flag_reason`, and the status. An
   `exists` write also sets `resolved_by = null`, `resolved_at = now`,
   `resolution = 'system'`.

`harvest`: make sure an `rfp_portal_harvest` job exists for the row
(`llm_queue.active_job`), else enqueue (priority 150); push
`next_attempt_at` by `RFP_HARVEST_POLL_SECONDS`. Locked logins push by the
lock remainder. Never calls the portal. An enqueue failure goes through the
attempt ladder; at the cap the row stays at `harvest` with `last_error`
and `next_attempt_at` one hour out (it is never lost).

### 2.4 The harvest job (`rfp_portal_ingest.execute_harvest(invitation_id, *, force=False)`)

`JOB_PORTAL_HARVEST = "rfp_portal_harvest"`, non-LLM, third claim pass,
`current_status` mapped from the harvest row so the AI monitor refuses to
requeue a finished one. Mirrors `rfp_harvest.execute` step for step:

1. Load the invitation (404 -> permanent). An `ignored` row is refused
   unless `force`: the person said not to open this bid on the portal, so a
   queued pipeline job that finds the row ignored returns without opening a
   session (logged at INFO). Pipeline mode requires `status = harvest`;
   manual mode (`force`, from "Harvest again" on a `done` row) never moves
   the status, only `harvest_id` / `harvested_at`.
2. Find or create the `rfp_harvests` row: `method = ngem`, `external_key =
   f"ngem:{agency_key}:{bid_number}"` (`agency_key` = the agency name
   lowercased, non-alphanumerics collapsed to `-`), `portal_invitation_id`
   set, `rfp_email_id` null. A `complete` row younger than
   `RFP_HARVEST_REUSE_DAYS` and not `force`: link and finish.
3. CAS the harvest row `pending|failed|complete -> running` with a fresh
   `claim_token`, OR from a `running` row whose `started_at` is older than
   `LLM_QUEUE_LEASE_SECONDS` (a dead worker's claim: nothing can still be
   writing under its token once the lease it held has expired;
   `rfp_harvest.stale_claim_filter`, shared with the Procore harvest). A
   losing CAS logs a warning and parks the invitation with a poll wait.
4. Facts: `session.fetch_event(view_url)` (the grid's link from the latest
   scan; a 302 to the login page means log in once and retry once; a
   `BadRequest.aspx` answer means the token is stale: refresh it by
   re-fetching page 1..N of the grid until the row is found, once, else
   permanent `stale_link`). Write `data`, `description_text` (the notes as
   text), `external_url` (the view URL is NOT stored: it is a bearer token
   for the session; store the entry URL instead) on the harvest row NOW
   (`facts_at`). `data` shape in section 4.
5. Files: `session.fetch_attachments(event)` pages the attachments grid;
   the rows are capped at `rfp_harvest_file_cap` and their declared sizes at
   `rfp_harvest_max_total_bytes` (over either cap is permanent, recorded
   with the counts). Create the sandbox run
   (`rfp_ingest.create_portal_run(portal_invitation_id, harvest_id)`,
   `source_kind = rfp_portal`, staging), then per file in grid order: renew
   the lease, pace, stream the `Extract.aspx` link into a scratch file under
   `rfp_ingest_max_file_bytes` (the filename from `Content-Disposition`,
   sanitized, falling back to the grid's name), read it back and
   `add_upload_file(..., source={"kind": "ngem", "file_name", "harvest_id"})`.
   Per-file entry `{file_name, description, size, sandbox_file_id, status,
   error}`; a download that fails after 3 paced attempts is
   `download_failed` and the loop continues (a lost queue lease, `_LeaseLost`
   from the renew seam, is never retried: the job stands down at once). Then
   `start_run` + `dispatch` (a run with zero accepted files is deleted; an
   empty grid records `no_files`). Resume: when the harvest row's
   `sandbox_run_id` points at a run still `staging`, the files the
   interrupted attempt already accepted (matched by name and declared size)
   keep their `sandbox_file_id` and are not downloaded twice; when that run
   is `pending` (started, never dispatched) nothing is downloaded and the
   run is dispatched.
6. Harvest row `complete`; invitation CAS `harvest -> done` with
   `harvest_id`, `harvested_at`, `attempts = 0`, `last_error = null`,
   `next_attempt_at = null`. When that CAS loses (the row was ignored or
   otherwise moved while the files were downloading) the harvest still
   happened: `harvest_id` and `harvested_at` are written field-scoped and
   the new status is left alone. A refreshed view link that answers
   BadRequest again ends the harvest permanently with `flag_reason =
   stale_link` like the first stale answer.

Failure mapping is the Procore one: locked/unavailable parks without an
attempt; transient rides the queue ladder; permanent marks the harvest row
`failed` with the sentence and moves the invitation to `done` with
`harvest_id` set. `mark_harvest_from_queue` does the permanent writes on the
queue's terminal failure, CAS-fenced.

### 2.5 Human actions (service functions, router in section 5)

All take `(sb, invitation_id, actor_id, ...)`, refuse the wrong status with
`RfpPortalError(code, message)` (409, `X-Error-Code`), refuse a reason
shorter than 3 characters with `rfp_portal_reason_invalid` (400: the
caller's input, never "the row moved"), audit `rfp_portal.<action>`, and
return the fresh detail. The router maps `RfpPortalError` only; any other
exception is a bug and answers 500.

| action | from | to | effect |
|---|---|---|---|
| `resolve_exists(project_id)` | `review_match` | `exists` | `match_project_id = project_id` (must be one of the stored candidates or any project in the window; validated), `resolution = 'human'`, `resolved_by`, `resolved_at`; the project marker appears |
| `resolve_new()` | `review_match` | `harvest` | `excluded_project_ids += match_project_id` (and every candidate the person saw, so the re-run cannot pick them), `resolution = 'human'`, attempts reset |
| `reopen(reason)` | `exists` | `match` | `excluded_project_ids += match_project_id`, `match_project_id = null`, `reopen_reason`, attempts reset; the project marker disappears |
| `ignore(reason)` | `match`, `review_match`, `harvest` | `ignored` | `ignored_by`, `ignored_at`, `ignore_reason`; a still-queued harvest job is canceled through `llm_queue.cancel` (the AI monitor's); a running one is left to finish (its writes are field-scoped; a `done` CAS from `harvest` loses because the status moved and the link is written field-scoped instead) |
| `unignore()` | `ignored` | `match` | attempts reset, `excluded_project_ids` kept |
| `harvest_again()` | `done` | `done` | enqueues `rfp_portal_harvest` with `force`; 409 while a job is active; 503 while logins are locked |
| `run_now()` | portal | run | 503 `rfp_portal_not_available` while the slice is inactive (`rfp_ngem_active` false, nothing inserted); inserts a manual run and enqueues the scan; 409 `rfp_portal_run_active`; 503 `rfp_portal_locked`; an insert that answers no row is read back through the active index; an enqueue that fails leaves the run parked for the scheduler and still answers it |

Concurrency: every action CASes on `(id, status)`; the sweep's writes are
field-scoped, so a person and the sweep cannot clobber each other's
decision (the loser's CAS returns no row and it re-reads).

---

## 3. NGEM client (`app/services/ngem_client.py`)

Pure `httpx` + `BeautifulSoup(lxml)`; no Supabase import except through the
store callbacks it is given; tests drive it with `httpx.MockTransport` and
the captured pages in `tests/fixtures_ngem/` (session tokens and the account
name scrubbed).

```
PROVIDER = "ngem"
class NgemConfig(username, password, entry_url, min_request_interval,
                 login_min_interval, timeout)
class NgemSession(config, store, *, on_lock=None, transport=None)
    ensure_session() -> None
    fetch_invitations(max_pages) -> InvitationsPage   # rows, total_items, pages_fetched
    fetch_event(view_url) -> EventFacts
    fetch_attachments(facts: EventFacts, max_pages) -> list[AttachmentRow]
    download(url, dest, max_bytes) -> DownloadResult   # bytes, filename
    close()
    availability_from(store_row) -> (ok, reason, locked_until)   # module function
InvitationRow(agency, bid_number_raw, bid_number, addendum_no, title,
              issued_on: date | None, close_at: datetime | None, time_left,
              bid_status, response_status, response_code, status_code,
              view_url)
EventFacts(bid_number_raw, title, bid_type, status, issued_at, close_at,
           question_cutoff_at, notes_html, notes_text,
           contact: {workgroup, name, address, phone, email},
           attachments_url, event_url)
AttachmentRow(index, file_name, size_bytes, description, download_url)
DownloadResult(bytes_written, filename)
parse_bid_number(raw) -> (bid_number, addendum_no | None)
parse_pt_datetime(text) -> datetime | None       # "9/17/2026 02:00 PM (PT)" and "8/17/2026 08:00:01 AM (PT)" -> aware America/Los_Angeles
parse_date(text) -> date | None                  # "8/25/2026"
parse_size(text) -> int | None                   # "(286 KB)", "(1.60 MB)", "(29.44 MB)"
parse_invitations_page(html) -> ParsedGrid       # rows, total_items, total_pages, pager_targets {page: __EVENTTARGET}, form_fields, form_action
parse_event_page(html) -> EventFacts
parse_attachments_page(html) -> ParsedAttachments   # rows, total_pages, pager_targets, form_fields, form_action
exceptions: NgemLoginFailed, NgemLoginLocked, NgemSessionExpired,
            NgemUnavailable, NgemForbidden (permanent), NgemTransient,
            NgemParseError (permanent)
ALLOWED_GET_PATHS, ALLOWED_POST_PATHS, ALLOWED_POSTBACK_TARGETS
```

### 3.1 Login (captured 2026-09-15)

1. `GET entry_url` -> 302 `/Login.aspx` -> 302 `/VendorLogin.aspx` -> 200
   the login form (`id="Form1"`, `action="./VendorLogin.aspx"`). Follow by
   hand, same host only (`supplier.ionwave.net`), at most 6 hops.
2. POST `VendorLogin.aspx` with EVERY hidden input of the form as served
   (`ScriptManager_TSM`, `__EVENTTARGET`, `__EVENTARGUMENT`, `__VIEWSTATE`,
   `__VIEWSTATEGENERATOR`, `__VIEWSTATEENCRYPTED`, the `*_ClientState`
   fields, `hdnCaptchaResponse`), plus `txtUserName`, `txtPassword`,
   `btnLogin=Login`, and `chkAgree_ClientState` set to the full Telerik
   client state: `{"text":<the checkbox text read from the page's RadCheckBox
   initializer, falling back to "I agree to the terms and conditions of using
   this website">,"value":"","enabled":true,"autoPostBack":true,
   "commandName":"","commandArgument":"","validationGroup":null,
   "checked":false}` serialized without spaces. The checkbox is hidden in
   the browser and posts unchecked; a partial JSON makes the server answer
   302 `/Error.aspx` (which also emails an error report to the portal's
   administrators, so failed attempts must stay rare). Never send the
   `hdnBtnLogin`, `hdnBtnMfa`, `btnForgotPassword` buttons. Browser-like
   headers: `Origin`, `Referer`, `Content-Type:
   application/x-www-form-urlencoded`, `Upgrade-Insecure-Requests`.
3. Success is a 302 whose `Location` is the entry URL (`ResponseList.aspx`)
   and a `procData` cookie; follow it and require the invitations grid on
   the page. 200 on `VendorLogin.aspx` again = credentials rejected
   (`NgemLoginFailed`, the `#divLoginMessage` text sanitized into the
   sentence); 302 to `/Error.aspx` = `NgemLoginFailed("unexpected page")`;
   a page mentioning the MFA prompt with the prompt window shown =
   `NgemLoginFailed("the account asks for MFA")`.
4. Save the jar (name, value, domain, path, expires, secure), `account =
   username`, `logged_in_at`; clear failures. `NGEM_LOGIN_MAX_FAILURES` (3)
   consecutive failures lock logins for `NGEM_LOGIN_LOCK_SECONDS` (21600)
   and ring `rfp_ngem.login_failed` to every IT Admin once ("NGEM login
   failed N times; scans and harvests are paused until <time>. Check
   NGEM_LOGIN_USERNAME / NGEM_LOGIN_PASSWORD."). Never two attempts within
   `NGEM_LOGIN_MIN_INTERVAL_SECONDS` (600), across processes: a chain that
   breaks on a 5xx or a timeout (`NgemTransient`) still stamps
   `last_login_attempt_at` (with `last_error = "transient: ..."`,
   `record_login(counts_toward_lock=False)`) so no other worker posts the
   credentials again inside the interval, without moving the failure
   counter.

### 3.2 Session rules

A login is attempted only from `ensure_session()`, only after a request
proved the session gone (a 302 to `/Login.aspx` or `/VendorLogin.aspx`, or a
200 on the login form where the grid was expected), never while locked.
The stored jar is loaded before every job (a stale jar fails one request and
triggers the one login). `last_used_at` is touched at most every 60 s. The
password never appears in a log line, an error message or a stored row.
The portal keeps sessions alive by a JS timer that plain HTTP does not run;
idle expiry is expected between the 6:30 and noon runs and costs one login.

### 3.3 Pages (captured shapes)

- Invitations grid: `table#ctl00_mainContent_ucInvitedListGrid_rgResponse_ctl00`
  (class `rgMasterTable`), `thead` columns `["", Agency, Bid Number, Title,
  Issue Date, Close Date, Time Left, Bid Status, Response Status, Response
  Status Code, Status Code]` matched BY HEADER TEXT, not position; body
  rows `tr.rgRow` / `tr.rgAltRow` whose first cell holds
  `a[id$=_aHrfView]` with the `VResponseEvent.aspx?e=` link; the pager in
  `tfoot` (`.rgPager`): page links `javascript:__doPostBack('ctl00$mainContent$ucInvitedListGrid$rgResponse$ctl00$ctl03$ctl01$ctlNN','')`
  (page 1 = `ctl05`, page 2 = `ctl06`, ...) and the text `37 items in 4
  pages`. The other grid (`ucNotInvitedListGrid`, Other Bid Opportunities)
  is never read. A page without the invited grid is `NgemParseError`.
- Paging: a full postback (no `__ASYNCPOST`) to the form's action with every
  non-button input of the form as served (hidden fields, the filter
  textboxes empty, the combobox client states as served) and
  `__EVENTTARGET` = the pager target; the client refuses any target not
  matching `ALLOWED_POSTBACK_TARGETS` (the invited grid's pager and the
  attachments grid's pager regexes) and never puts text into a filter
  field.
- Event Details (`VResponseEvent.aspx?e=`): the "Bid Information" table's
  label cells (`Bid Type`, `Status`, `Issue Date & Time`, `Close Date &
  Time`, `Question Cuttoff Date & Time` (sic), `Notes`) and the "Bid
  Contact Information" table's (`Workgroup`, `Contact Name`, `Address`,
  `Contact Phone`, `Contact Email`), matched by label text; the header line
  `5584-GS Addendum 1 (Emergency Phone Tower Installations and Upgrades)`;
  the tab strip links (`a.rtsLink`) give the `VResponseBidAttachments.aspx`
  URL. Notes are HTML: stored as text through `rfp_harvest.html_to_text`
  and, capped, as the sanitized HTML (`nh3` with a small allowlist: p, br,
  b, strong, i, em, u, ul, ol, li, a[href https only]) for display.
- Attachments (`VResponseBidAttachments.aspx?e=`): `table#ctl00_mainContent_rgBidAttachments_ctl00`,
  header `[#, Download All, File Name, Description]`, rows with
  `a[id$=_lnkDownload]` (`https://supplier.ionwave.net/Extract.aspx?e=`),
  the file cell `Name.pdf (286 KB)` (name and size parsed apart), the
  description cell; pager as above (`ctl00$mainContent$rgBidAttachments$ctl00$ctl03$ctl01$ctlNN`).
  The "Download All" anchor (`hfDocZipAction` + `__doPostBack('','')`) is
  never followed.
- Download: `GET Extract.aspx?e=` with the jar, `Referer` the attachments
  page; 200 with `Content-Disposition: attachment;filename="..."` streamed
  in 1 MB chunks under the running cap into `dest` (opened `O_EXCL`),
  unlinked on every failure; a 302 to the login page is `NgemSessionExpired`
  (one login, one retry); an HTML answer is `NgemTransient` once, then
  `download_failed`.

### 3.4 Allowlist and pace

`ALLOWED_GET_PATHS`: `/VendorResponse/ResponseList.aspx`, `/Login.aspx`,
`/VendorLogin.aspx`, `/VendorResponse/Bid/VResponseEvent.aspx`,
`/VendorResponse/Bid/VResponseBidAttachments.aspx`, `/Extract.aspx`.
`ALLOWED_POST_PATHS`: `/VendorLogin.aspx` (the login), `/VendorResponse/
ResponseList.aspx` and `/VendorResponse/Bid/VResponseBidAttachments.aspx`
(pager postbacks only). Anything else raises `ValueError` before the
request is built and is a test failure, not a runtime branch. Portal-
controlled URLs never reach that check: a redirect `Location` or a form
`action` that is not https on the portal host at an allowed page is refused
as `NgemForbidden` ("the portal sent the client somewhere it will not go";
`NgemLoginFailed` inside the login chain, where it counts as a failed
login), and a page body is streamed and stopped at 4 MB
(`NgemParseError`), never buffered whole first. In
particular `VResponseSubmission.aspx`, `VResponseQuestion.aspx`,
`VResponseAttachments.aspx` (our response uploads), `VResponseLine.aspx`,
the profile pages and every other tab are unreachable from code.

`_pace()`: a jittered gap of at least `NGEM_MIN_REQUEST_INTERVAL_SECONDS`
(2.0, up to 2x) between any two portal requests in the process, under a
module lock; applies to logins, pages, postbacks and downloads alike.

---

## 4. Data model (migration 0126)

`rfp_portal_invitations`

| column | notes |
|---|---|
| id | uuid pk |
| portal | text not null (`ngem`) |
| agency | text not null |
| agency_key | text not null (derived, see 2.4) |
| bid_number | text not null (addendum suffix stripped) |
| bid_number_raw | text not null (as shown) |
| addendum_no | int |
| title | text not null |
| issued_on | date |
| close_at | timestamptz |
| time_left, bid_status, response_status, response_code, status_code | text |
| view_url | text (session-bound; refreshed each scan; never returned by the API) |
| status | text not null check in (`match`, `harvest`, `review_match`, `exists`, `done`, `ignored`) |
| flag_reason, decided_at_step | text |
| attempts int, next_attempt_at timestamptz, last_error text | the per-step ladder |
| match_candidates jsonb, match_project_id uuid references projects on delete set null, match_score numeric(5,3), match_llm_model, match_llm_prompt_version, match_weights jsonb, matched_at | the evaluation |
| possible_rebid_project_id uuid, possible_rebid_score numeric(5,3) | |
| excluded_project_ids uuid[] not null default '{}' | |
| resolution text check in (`system`, `human`), resolved_by uuid references profiles, resolved_at | how `exists` was reached |
| reopen_reason text | |
| ignored_by uuid, ignored_at, ignore_reason | |
| harvest_id uuid references rfp_harvests on delete set null, harvested_at | |
| change_log jsonb not null default '[]' | `[{at, field, old, new, run_id}]`, capped 50 |
| first_seen_at, last_seen_at timestamptz not null; first_seen_run_id, last_seen_run_id uuid references rfp_portal_runs on delete set null; seen_count int; missing_since timestamptz | |
| created_at, updated_at | |

Unique `(portal, agency_key, bid_number)`. Indexes: `(portal, status,
next_attempt_at)`, `(match_project_id) where match_project_id is not null`,
`(close_at desc)`.

`rfp_portal_runs`

| column | notes |
|---|---|
| id | uuid pk |
| portal | text not null |
| trigger | text not null check in (`scheduled`, `manual`) |
| scheduled_for | timestamptz (null for manual) |
| requested_by | uuid references profiles on delete set null |
| status | text not null check in (`queued`, `running`, `complete`, `failed`) |
| claimed_by, started_at, finished_at | |
| pages_scanned, invitations_seen, invitations_new, invitations_changed, invitations_skipped_closed | int default 0 |
| last_error | text |
| next_attempt_at | timestamptz |
| created_at, updated_at | |

Unique `(portal, scheduled_for) where scheduled_for is not null`; partial
unique `(portal) where status in ('queued', 'running')`.

`rfp_harvests`: `rfp_email_id` becomes nullable; `portal_invitation_id uuid
references rfp_portal_invitations on delete cascade`; check `(rfp_email_id
is not null) <> (portal_invitation_id is not null)`; `method` gains `ngem`
(the existing check, if any, widened). `data` for NGEM:

```
{
  "platform": "ngem", "agency", "bid_number", "bid_number_raw", "addendum_no",
  "title", "bid_type", "bid_status",
  "issued_at", "close_at", "question_cutoff_at" (ISO instants),
  "contact": {"workgroup", "name", "address", "phone", "email"},
  "notes_html" (sanitized, capped),
  "documents": {"count", "bytes"}
}
```

`files[]` entries: `{index, file_name, description, size, sandbox_file_id,
status (accepted|rejected|too_large|download_failed|skipped_cap), error}`.
Download URLs are never stored.

`rfp_harvest_sessions`: a second row, `provider = ngem`. No DDL.

`rfp_ingest_runs`: `source_kind` check gains `rfp_portal`;
`portal_invitation_id uuid references rfp_portal_invitations on delete set
null`. `rfp_ingest_files.manifest.identity.source` for these files is
`{kind: "ngem", file_name, harvest_id}`.

`llm_jobs.job_type` check gains `rfp_portal_scan`, `rfp_portal_harvest`.

All new tables: RLS enabled and forced, no policies; `set_updated_at`
trigger; idempotent DDL; `notify pgrst, 'reload schema'` at the end.

---

## 5. API (`app/routers/rfp_portal.py`, prefix `/rfp-portal`, review-queue roles, feature switch `rfp_ngem_enabled` on top of `rfp_ingest_enabled`)

| method and path | purpose |
|---|---|
| GET /rfp-portal/invitations?portal=ngem&view=review\|new\|existing\|ignored\|all&q=&limit=&offset= | list rows (`view` maps to statuses: review = `review_match`; new = `match`, `harvest`, `done`; existing = `exists`; ignored = `ignored`), newest `close_at` first inside each view; each row `{id, agency, bid_number, bid_number_raw, addendum_no, title, close_at, issued_on, status, flag_reason, response_status, match_project (id, name, number) or null, match_score (redacted for engineers), harvest_status, last_seen_at, missing_since, changed_recently (a change_log entry in the last 7 days)}` plus `counts` per view |
| GET /rfp-portal/invitations/{id} | the detail: the row without `view_url`, `match_candidates` redacted per role (`rfp_match.redact_candidates`), `excluded_projects` (names), `change_log`, `harvest` (the harvest row without `raw`/`claim_token`), `harvest_job` (queue poll info), `possible_rebid` |
| POST /rfp-portal/invitations/{id}/resolve `{project_id}` | 2.5 `resolve_exists` |
| POST /rfp-portal/invitations/{id}/new | `resolve_new` |
| POST /rfp-portal/invitations/{id}/reopen `{reason}` | `reopen` |
| POST /rfp-portal/invitations/{id}/ignore `{reason?}` | `ignore` |
| POST /rfp-portal/invitations/{id}/unignore | `unignore` |
| POST /rfp-portal/invitations/{id}/harvest `{force?}` | 202 `{job}`; 409 `rfp_portal_harvest_active`; 503 `rfp_portal_locked` |
| POST /rfp-portal/ngem/runs | Run now: 202 `{run}`; 409 `rfp_portal_run_active`; 503 `rfp_portal_locked` |
| GET /rfp-portal/ngem/status | `{enabled, configured, account, logged_in_at, last_used_at, last_login_attempt_at, login_failures, locked_until, last_error, schedule_times, next_run_at, last_run (the latest run row), active_run, active_jobs}`; never cookies, never the password |

Error codes (`core/error_codes.py`, `docs/ERROR_CODES.md`):
`rfp_portal_not_actionable`, `rfp_portal_project_required`,
`rfp_portal_harvest_active`, `rfp_portal_run_active`, `rfp_portal_locked`,
`rfp_portal_not_available` (409; 503 from Run now while the slice is
inactive), `rfp_portal_reason_invalid` (400). Rate limit: the default
bucket. Every mutation audited (`rfp_portal.<action>`). The list's per-view
`counts` come from one exact-count HEAD query per view, never from
transferred rows; the detail's `harvest.files` entries carry the section 4
keys only.

Project side: `GET /projects/{id}` gains `portal_sources: [{portal,
invitation_id, agency, bid_number, bid_number_raw, addendum_no, close_at,
resolution, resolved_at}]` (rows at `exists` with `match_project_id = id`),
served by `rfp_portal_ingest.portal_sources_for_projects(sb, ids)`; the
dashboard list is unchanged. Bell payloads route: `rfp_ngem.login_failed`
-> `/settings/rfp-ingestion`; `rfp_ngem.new_invitations` ->
`/rfp-emails?tab=ngem`; `rfp_ngem.invitation_changed` ->
`/rfp-emails?tab=ngem&invitation=<id>`; `rfp_ngem.scan_failed` (every IT
Admin when a scan run ends `failed`, metadata `{run_id, trigger, error}`)
-> `/settings/rfp-ingestion`.

---

## 6. Frontend

- `/rfp-emails` gains the tab `ngem` (label "NGEM") after `matches`, with a
  badge = the review count. Inside: view pills Needs review / New / Existing
  / Ignored / All (counts), a search box (agency, bid number, title), a table
  (Agency, Bid number with an "Addendum N" chip, Title, Close date in
  Pacific, Status chip, Match / Harvest chips, Last seen, a "changed" dot),
  pagination like the other tabs, `?invitation=<id>` opens the detail.
- Detail modal: the facts grid (agency, bid number, title, issue, close,
  response status, last seen, missing since), the change log, the Match
  block (best candidate with the score, the candidate list with the
  breakdown the email drawer shows, redacted for engineers, the rebid
  hint), actions per status (2.5) with confirm dialogs where a reason is
  taken, the Project data card (the existing `RfpHarvestBlock` refactored
  so its inner card takes `{harvest, harvest_job, available, locked}` and
  both the email detail and this modal render it), the sandbox link under
  the sandbox gate. The modal polls every 5 s while a job is active.
- Settings, RFP Ingestion tab: a "NGEM" block: configured (the account,
  never the password), last login, lock state, last error, schedule times,
  next run, last run (when, counts, error), Run now (disabled while a run is
  active or logins are locked, with the sentence).
- Project page: an "NGEM" pill next to the RFP matches pill when
  `portal_sources` is non-empty (title = agency + bid number + close date),
  and a "Portal invitations" section inside `RfpMatchesModal` listing them
  with a link to the NGEM tab detail.
- Bells: the three types route as in section 5.
- All strings in all six catalogs; no em dashes; `npm run lint`, `tsc`,
  `next build` green.

---

## 7. Security notes

- Portal content is attacker-adjacent (an agency types it) and the files
  are attacker-controlled bytes: files go to the sandbox like any upload;
  the notes are stored as text and as an allowlisted HTML fragment; every
  stored string is capped.
- The client can only fetch the allowlisted pages, post the login and the
  two pagers; the response, question, submission and profile pages are not
  reachable from code, and a test asserts every URL the service would fetch
  against the allowlist. Redirects are followed by hand and only within
  `supplier.ionwave.net`.
- The password lives in the env and in the login POST body only. The
  cookie jar is a bearer credential for the account: forced RLS, service
  role only, never returned by any route, replaced on every login, and the
  `account` column ties it to the username so a rotated env drops it.
  `view_url` and download URLs are session-bound tokens and are never
  returned by the API.
- The manual routes use the default rate bucket; jobs are not
  user-triggered except through them.
- Failed logins email the portal's administrators (their `/Error.aspx`);
  the lock and the minimum interval keep that to a handful per incident.

---

## 8. Configuration

| env | default | meaning |
|---|---|---|
| RFP_NGEM_ENABLED | false | this slice; `RFP_INGESTION_ENABLED` still gates everything |
| NGEM_LOGIN_USERNAME / NGEM_LOGIN_PASSWORD | empty | the supplier account; empty = feature inert (`ngem_configured` false) |
| NGEM_ENTRY_URL | empty | the `ResponseList.aspx?e=...` link; must be `https://supplier.ionwave.net/...` |
| RFP_NGEM_SCHEDULE_TIMES | 06:30,12:00 | Pacific `HH:MM` list, 1 to 12 entries, validated |
| RFP_NGEM_CATCHUP_HOURS | 4 | a missed slot still runs inside this window |
| RFP_NGEM_POLL_SECONDS | 60 | scheduler and sweep tick |
| RFP_NGEM_AUTO_RESOLVE_ENABLED | true | confident matches resolve to `exists` without a click |
| RFP_NGEM_MAX_LIST_PAGES | 20 | grid pages per scan |
| RFP_NGEM_SCAN_TIMEOUT_SECONDS | 1800 | a `running` run with no job past this is `failed` |
| RFP_NGEM_SWEEP_BATCH | 50 | invitations per tick |
| RFP_NGEM_QUEUE_PRIORITY | 150 | the `rfp_portal_harvest` job type |
| RFP_NGEM_SCAN_QUEUE_PRIORITY | 140 | the `rfp_portal_scan` job type: ahead of the harvests (the two share one third-pass slot per worker; lower runs first) |
| NGEM_MIN_REQUEST_INTERVAL_SECONDS | 2.0 | pace floor (x 1.0 to 2.0) |
| NGEM_LOGIN_MIN_INTERVAL_SECONDS | 600 | |
| NGEM_LOGIN_MAX_FAILURES | 3 | |
| NGEM_LOGIN_LOCK_SECONDS | 21600 | |
| NGEM_REQUEST_TIMEOUT_SECONDS | 30 | |

Reused: `RFP_HARVEST_CONCURRENCY`, `RFP_HARVEST_POLL_SECONDS`,
`RFP_HARVEST_REUSE_DAYS`, `RFP_HARVEST_MAX_FILES`,
`RFP_HARVEST_MAX_TOTAL_BYTES`, every `RFP_MATCH_*` weight and threshold,
`RFP_EMAIL_INGESTION_CLASSIFY_MAX_ATTEMPTS` (the per-step cap),
`LLM_QUEUE_LEASE_SECONDS` (a `running` harvest claim older than this is a
dead worker's and is taken over). Derived: `Settings.ngem_configured`,
`Settings.rfp_ngem_active`, `Settings.rfp_ngem_schedule` (parsed `(hour,
minute)` tuples).

---

## 9. Tests

- `tests/test_ngem_client.py` (MockTransport, fixtures from the capture):
  the login chain (entry 302 302 200, the POST field set including the
  exact `chkAgree_ClientState`, success 302 to the entry URL; credentials
  rejected; `/Error.aspx`; MFA prompt; an off-site redirect refused); the
  grid parser over the real page 1 and page 2 (37 items, 4 pages, the
  pager targets, every column by header, `parse_bid_number` over the
  captured numbers incl. the Henderson one, `parse_pt_datetime`,
  `parse_size` for KB/MB); the paging postback (target allowlist, no filter
  text, full postback); the event page parser (labels, notes text and
  sanitized HTML, the attachments URL); the attachments parser (8 rows,
  names, sizes, descriptions, links); download (Content-Disposition
  filename, byte cap, unlink on failure, session expiry -> one login -> one
  retry); the lock counter and its bell; the pace (patched clock); the
  allowlist.
- `tests/test_rfp_portal_ingest.py`: slot computation across a DST change
  and the catch-up window; the run ledger claim (23505 = someone else's);
  scan upsert (new, known unchanged, changed title / close date / addendum,
  past-close skipped, missing_since set and cleared, change_log cap, the
  bells and their dedupe, the ignored row's silence); the match step
  (exists / review_match / harvest routing, the switch off, model down
  waits without an attempt, ladder to `match_failed`, unusable twice,
  excluded ids, rebid hint, no-name title); the harvest step and job
  (facts before files, caps, per-file outcomes, run created / deleted when
  empty, every failure mapping, losing claim CAS, `mark_*_from_queue`
  fencing, stale link refresh); every human action's status guard and
  writes; `portal_sources_for_projects`.
- `tests/test_llm_queue.py`: the two job types in the third pass and their
  `current_status` mappings.
- Router tests: every route's answers and codes; the detail never carries
  `view_url`, a download URL, cookies or the password; the engineer
  redaction; the project detail's `portal_sources`.
- Live on dev: Run now, a scan over the real grid, one harvest of an
  already-viewed bid (the UNLV 5584-GS invitation), the tab and modal
  rendered headless.

---

## 10. Out of scope

- Automatic re-harvest when an addendum appears (manual "Harvest again").
- The creation step that turns a harvest into a project.
- A second portal (the tables and the job types are ready; a client and a
  `portal` registry entry are what it takes).
- Production migration and Railway variables (explicit approval).

---

## 11. Build record and live run (2026-09-16, dev database)

Backend: `supabase/migrations/0126_rfp_portal_invitations.sql` (applied to
the dev project and confirmed: both tables, RLS enabled and forced, the
`set_updated_at` triggers, `rfp_harvests.rfp_email_id` nullable plus
`portal_invitation_id` and the exactly-one-source check, the sandbox's
`rfp_portal` source kind and `portal_invitation_id`, the two job types),
`app/core/config.py` (every section 8 setting, the `HH:MM` schedule parser,
the `supplier.ionwave.net` entry-URL guard, `ngem_configured`,
`rfp_ngem_active`, `rfp_ngem_schedule`), `app/services/rfp_portal_ingest.py`
(scheduler, scan job, sweep, harvest job, session store, bells, human
actions, reads, the `PORTALS` registry), `app/services/ngem_client.py` (the
pure client, its own record), `app/services/llm_queue.py` (`JOB_PORTAL_SCAN`,
`JOB_PORTAL_HARVEST`, the third claim pass shared with the Procore harvest),
`app/services/rfp_ingest.py` (`create_portal_run`), `app/services/
rfp_email_ingest.py` (`acquire_lease`, `renew_lease`, `reference_bundle`,
`split_system` made public with the private aliases kept),
`app/routers/rfp_portal.py`, `app/routers/projects.py` (`portal_sources`),
`app/models/schemas.py` (`PortalSourceOut`), `app/core/features.py`
(`rfp_ngem` in GET /features: the two switches `RFP_INGESTION_ENABLED` and
`RFP_NGEM_ENABLED`, exactly the router's gate, corrected in section 12 from
the earlier "on AND configured": an unconfigured account must not hide the
settings block that says so; the scheduler loop and the queue's third claim
pass keep using `rfp_ngem_active`), `app/main.py` (the
polling loop behind `rfp_ngem_active`, the boot line), `core/error_codes.py`,
`docs/ERROR_CODES.md`, `.env.example`. Tests: `tests/test_ngem_client.py`,
`tests/test_rfp_portal_ingest.py` (68), `tests/test_rfp_portal_router.py`
(27) plus additions to the queue, feature-flag and projects suites; the full
suite green.

Clarifications and deviations, all deliberate:

- `ngem_configured` requires the entry URL as well as the two credentials:
  without it the client cannot reach the grid, so the feature is inert
  rather than failing every scan. The configured entry URL's doubled slash
  (the portal's own link reads `supplier.ionwave.net//VendorResponse/...`)
  is collapsed before it reaches the client's strict path check.
- The scan job accepts a run at `running` as well as `queued`: the queue's
  retry ladder re-runs the same job against a run the doc leaves at
  `running` with `last_error`.
- The scheduler also fails a `queued` run with no job and no wait past
  `RFP_NGEM_SCAN_TIMEOUT_SECONDS` (not only a `running` one): the partial
  unique index means a stranded queued run would block every later run.
  Yesterday's slots are computed alongside today's so a late slot's
  catch-up window survives midnight; the ledger dedups.
- `route(...)` can only answer `duplicate`, `merged`, `review_match` and
  `done`; the mapping table covers all four, and anything else would wait at
  `review_match`.
- `harvest_again` is allowed from `harvest` too (the pipeline's own job,
  sooner); the route's `force` defaults to true.
- "Not a match" clears `match_project_id` (the candidates and the score stay
  as the evaluation record) so a `done` row never reads as matched, and runs
  the harvest step inline.
- Bells are bell rows only (`mirror_email=False`), like the Procore lock
  bell; `notification_email._TYPE_META` carries their headings.
- The detail and the status route add `resolved_by_name`, `ignored_by_name`
  and `requested_by_name` for the frontend.

Live run, dev server, the real portal, the company's account:

- Scheduled path: the 06:30 Pacific slot was claimed by the scheduler the
  moment the loop started inside its catch-up window; the first attempt
  parked the run on the entry-URL check (the doubled slash, fixed as above),
  the re-queued attempt logged in once (about 20 s at the pace), paged the
  grid (4 pages, 37 items plus one) and inserted 38 invitations at `match`
  in 39 s (run `a763c47b`, 38 seen / 38 new / 0 changed / 0 closed). The
  new-invitations bell rang once per review role.
- Match step: the UNLV 5584-GS invitation ("Emergency Phone Tower
  Installations and Upgrades") found no candidate above the review
  threshold and moved to `harvest` with `no_candidate`; the harvest step
  enqueued its job in the same pass. The other 37 invitations were parked
  as `ignored` (reason "Parked by the build session: not the live-check
  invitation.") by SQL BEFORE the sweep reached them, so no other bid was
  opened on the portal; un-park them with one UPDATE back to `match` when
  real harvests are wanted.
- Harvest: facts first (`facts_at` before the first download, `data`
  with the contact, the sanitized notes, `documents {8, 50.6 MB}`), then
  the eight attachments into sandbox run `d16aa0c9` (`source_kind =
  rfp_portal`, per-file source `{kind: "ngem", file_name, harvest_id}`).
  The dev server reloaded mid-download (a file edit); the queue requeued
  the job as interrupted, the harvest row was reset to `pending` by hand
  (the losing-claim park would otherwise wait on the dead worker's claim
  forever, the same gap the Procore harvest has), and the second attempt
  resumed past the four accepted files: 8 of 8 accepted, harvest
  `f317aba9` complete, invitation `f72a49fb` at `done` with `harvest_id`,
  `harvested_at`, `attempts 0`, `flag_reason` kept. One login for the
  whole session (the stored jar served every later request).
- Routes, as the E2E account: `POST /rfp-portal/ngem/runs` 202 (manual run
  `24df9ead`, 4 pages, 38 seen, 0 new, 0 changed, 17 s) and 409
  `rfp_portal_run_active` while it ran; `GET /rfp-portal/ngem/status`
  (account, one login, no lock, schedule, next run, last run with
  `requested_by_name`, 0 active jobs); the list (counts, `harvest_status`,
  `match_project`, `changed_recently`) and the search; the detail with no
  `view_url`, no `VResponseEvent` or `Extract.aspx` link, no cookies, no
  `claim_token`, no `raw`; harvest on an ignored row 409
  `rfp_portal_not_available`; resolve on a done row 409
  `rfp_portal_not_actionable`; a missing or malformed id 404. A stray
  second invocation of the check script ran one more manual scan (run
  `7944c773`, 0 new): three real scans in all, one real harvest.

Open items:

- A worker that dies mid-harvest: FIXED in section 12 (item 1); the note
  stays here as the record of what the live run hit.
- The sweep lease (`rfp_email_ingestion_lease_seconds`, stretched to cover
  one LLM call) outlives a dead holder by up to that length after a
  restart; the email sweep has the same property.

---

## 12. Review round 2026-09-16 (two adversarial reviews, every finding fixed on dev)

Correctness and concurrency:

1. Dead-worker harvest claim: `_claim_harvest` (both harvests, through
   `rfp_harvest.stale_claim_filter`) also takes a `running` row whose
   `started_at` is older than `LLM_QUEUE_LEASE_SECONDS`; a losing claim logs
   a warning.
2. Scheduled slot / Run now: an enqueue failure parks the run instead of
   failing it; every `queued` run with no scan job is dispatched once its
   wait has passed, or at once when it has none; `_fail_stale_runs` stays
   the cap.
3. `RFP_NGEM_SCAN_QUEUE_PRIORITY` (140): the scan no longer queues behind
   the harvest backlog at 150.
4. `rfp_ngem.scan_failed` to every IT Admin when a run ends `failed`
   (deduped while unread, `last_error` capped; route
   `/settings/rfp-ingestion`), plus INFO lines for slot claimed, run
   dispatched, run complete with counts, run failed with the sentence.
5. Ignore: a queued pipeline harvest that finds the row `ignored` returns
   without opening a session (only `force` may); `ignore()` cancels a
   still-queued harvest job through `llm_queue.cancel`; a `harvest -> done`
   CAS that loses falls back to the field-scoped link.
6. Bell dedupe per user: `new_invitations` per `(type, user, run_id)`,
   `invitation_changed` per `(type, user, invitation_id)` (one row per user
   through `notify_user`); the login and scan-failed bells stay per type.
7. Run-row writes fenced on `(id, status, claimed_by)` with a per-attempt
   claim token; the new-invitations bell runs after the run is complete
   inside its own try; a retry of a `complete` run returns normally.
8. `match_weights` gains `auto_resolve_enabled` (the switch this step
   routed by); `settings_snapshot` itself is unchanged.
9. List counts: one exact-count HEAD query per view (`view_counts`).
10. The sweep queries per portal (`portal = ...`) so the sweep index applies.
11. Resume: accepted files carried over by name and size when the staging
    run is reused; a `pending` (started, undispatched) run is dispatched;
    the same in the Procore harvest (`carry_over_accepted`).
12. A lost queue lease is `_LeaseLost`, never retried by the per-file loop.
13. Run now answers 503 `rfp_portal_not_available` while `rfp_ngem_active`
    is false, before inserting anything.
14. An empty title is stored empty (no "(untitled)" placeholder); the
    no-name exit takes it.
15. Same-scan key collisions are merged (first row wins, logged); the empty
    agency fallback ("Unknown agency" / `unknown-agency`) is shared by the
    scan and the stale-link re-find (`agency_name`, `_row_key`).
16. Tests for every item, and the router's "no download URL" test runs the
    real read path over a fake row carrying a `view_url` and an
    `Extract.aspx` link in `harvest.files` (the read path now whitelists the
    file-entry keys).

Security and exposure:

17. `rfp_portal_reason_invalid` (400) for a short or missing reason.
18. `GET /features` `rfp_ngem` is the two switches, not the configured or
    queue derivation.
19. Bell text capped: each portal value 80 characters, the title 120, the
    message 600 (all three bell composers).
20. The router maps `RfpPortalError` only (a `KeyError` or `IndexError` is a
    500); `run_now` survives an insert that answers no row.
21. A transient failure on the login chain stamps `last_login_attempt_at`
    (`record_login(counts_toward_lock=False)`) so no other process posts the
    credentials inside the interval.
22. `NgemConfig.password` is `field(repr=False)`.
23. A redirect `Location` or form `action` with the wrong scheme or off the
    allowlist is `NgemForbidden` (`NgemLoginFailed` in the login chain),
    never a `ValueError`.
24. `normalize_entry_url` collapses `//` in the path only; the `e=` token is
    untouched.
25. Every parsed row's key is added to `seen` before the past-close skip.
26. A refreshed link that answers BadRequest again keeps `flag_reason =
    stale_link`.
27. Page bodies are streamed and stopped at 4 MB (`NgemParseError`) in
    `_request`, which every page fetch and the login chain go through.
28. `sanitize_notes_html` allows `https` only.

Tests after the round: `tests/test_rfp_portal_ingest.py` (93),
`tests/test_rfp_portal_router.py` (34), `tests/test_ngem_client.py` (171),
`tests/test_rfp_harvest.py` (+4); the full suite green apart from the
sibling PipelineSuite work in progress at the time of the run.
