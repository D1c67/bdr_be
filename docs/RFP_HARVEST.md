# RFP Ingestion: Project Data Harvest (Procore first)

Design record for the fourth slice of RFP Ingestion: after the match step
finds no existing project, go to the platform the invitation came from,
pull the project facts and the bidding documents, and park them where the
later creation slice (and a person, today) can look at them. This sprint
built the Procore harvester; the harvester interface is method-keyed, and
the second harvester behind it, PipelineSuite (section 2.4,
`RFP_PIPELINESUITE.md`, 2026-09-16), slotted in without touching the
pipeline, as SmartBid and organic (attachment) harvests can later.
(BuildingConnected and NGEM since 2026-09-15, and PlanHub since
2026-09-16, are not invitation methods: their mail is dropped at listing
time, see `RFP_EMAIL_INGESTION.md` section 3.1.)

Status: design v1, 2026-09-14, written from a live capture of the Procore
bid sheet for the Warehouse HVAC Upgrade invitation (Monument Construction,
rfp_emails `fe1ec6ab-e252-4ab4-b025-ca7eae7e75eb`, bid 64706611). BUILT
2026-09-14/15 and verified live on the dev database (section 11). Migration
0123 applied to dev only. Nothing here creates projects or sends mail.

Naming, used everywhere: setting prefix `rfp_harvest_` (env prefix
`RFP_HARVEST_`) plus `procore_` (env `PROCORE_`), pure client
`app/services/procore_client.py`, service `app/services/rfp_harvest.py`,
migration `0123_rfp_harvest.sql`, tables `rfp_harvests` and
`rfp_harvest_sessions`, queue job type `rfp_harvest`, pipeline status
`harvest`, sandbox run source kind `rfp_email`, bell types
`rfp_harvest.login_failed`, FE card "Project data" on the `/rfp-emails`
detail, FE namespace `rfpEmails.harvest`.

---

## 1. Decisions locked in (2026-09-14)

| Topic | Decision |
|---|---|
| Where it runs | A new pipeline status `harvest` between `match` and `done`. The match step's two "no existing project" exits (`no_project_name` and no candidate) land at `harvest` when the row's invitation method has a harvester and the email carries a usable platform link; every other row goes to `done` exactly as today. `merged` and `duplicate` rows are not harvested in this sprint (the project exists; its documents come later). Sibling followers (`flag_reason = sibling`) never harvest; the leader does. |
| How it runs | The `harvest` step only enqueues a queue job (`rfp_harvest`, one per email row, third claim pass, concurrency `RFP_HARVEST_CONCURRENCY` = 1 per worker) and the row waits at `harvest`. The job does the scraping and the downloads, so the sweep tick is never blocked by a 100 MB drawing set. Crash-resume is the sweep re-enqueueing a `harvest` row that has no active job. |
| Access | Plain HTTP (`httpx`) with a logged-in Procore session. Measured 2026-09-14: the bid sheet's own JSON endpoints answer to the session cookies without any browser, the login is a Rails form, and every document is a signed `storage.procore.com` URL that downloads with no cookies at all. No headless browser, no Playwright dependency, no image change on Railway. |
| Credentials | `PROCORE_LOGIN_EMAIL` and `PROCORE_LOGIN_PASSWORD` in the env (a personal account today; change the env to change the account). Empty means "no Procore harvester": rows go to `done` as before. No MFA on the account. |
| Session | One persisted session for the whole deployment (`rfp_harvest_sessions`, provider `procore`, cookies stored under forced RLS), shared by both production workers and surviving restarts, so the account is logged in once and reused. Login happens only when a request proves the session is gone (401 or a redirect into `login.procore.com`), never pre-emptively, never more than once per `PROCORE_LOGIN_MIN_INTERVAL_SECONDS` (600). `PROCORE_LOGIN_MAX_FAILURES` (3) consecutive login failures lock logins for `PROCORE_LOGIN_LOCK_SECONDS` (21600) and ring the IT Admin's bell once; harvests wait, no attempt spent. |
| Pace | Human pace, enforced in one place (`procore_client._pace`): a jittered gap of at least `PROCORE_MIN_REQUEST_INTERVAL_SECONDS` (2.0, up to 2x) between any two Procore requests in the process, downloads sequential, one harvest at a time per worker, browser-like headers. A whole 72-file set takes a few minutes, which is what a person clicking through would take. |
| Never touched | Bid intent links (`/intents/public_set_bid_intent`, "Will Bid" / "Will Not Bid"), submit, NDA sign, "Email documents", uploads, anything that is not a GET, apart from the two login POSTs. The allowlist is code (`procore_client.ALLOWED_PATHS`) and a test asserts every URL the client would fetch against it. |
| What is harvested | The bid package (`/rest/v1.0/companies/{c}/bid_packages/{p}`), the bid (`/rest/v1.0/companies/{c}/bids/{b}?view=planroom_redesign`), the bid form (`/rest/v1.0/companies/{c}/bid/{b}/bid_forms/{f}`) and the documents manifest (`/rest/v1.0/companies/{c}/planroom/bid_packages/{p}/documents`), normalized into one `data` document (section 4). The description is the bid package's `bid_email_message`; the bidding instructions its `bid_web_message`; both HTML, stored as text. |
| Files | Every manifest row is downloaded from its signed `s3_source` and handed to the Ingestion Sandbox as one run (`source_kind = rfp_email`), through the same `add_upload_file` path the sandbox page uses, so PDFs are rendered and verified and anything else is recorded as `rejected/not_pdf`. The zip route (`download_bid_docs_zip`) is not used: the manifest gives every file with its path, size and discipline, and a zip would need its own bomb defenses. |
| Dedup | One `rfp_harvests` row per platform object (`external_key`, for Procore `procore:{company_id}:{bid_id}`). A later email for the same bid (the daily reminder, an addendum notice, a second recipient copy) links to a `complete` harvest younger than `RFP_HARVEST_REUSE_DAYS` (14) and goes to `done` without touching Procore. "Harvest again" on the detail screen refreshes it (new sandbox run, same harvest row). Detecting a changed manifest automatically is a later step. |
| Failure | A harvest never blocks the pipeline forever. Transient trouble (network, 5xx, a locked login) waits or retries; a permanent problem (no link, a bid the account cannot see, over the file caps) marks the harvest `failed` with a user-facing sentence and moves the email to `done`. A person retries from the detail screen. |
| Who sees it | The review-queue roles (`RFP_REVIEW_ROLES`) see the "Project data" card and may run "Harvest project data" / "Harvest again". The sandbox run link needs the dev gate the sandbox page already has. |

---

## 2. Pipeline

```
... -> match
        |  no existing project
        |    method has a harvester and the email has a platform link -> harvest
        |    otherwise                                                 -> done (as today)
        v
   harvest    step: nothing but "make sure a job exists" (enqueue, or re-enqueue
              after a crash); the row waits with next_attempt_at pushed
              RFP_HARVEST_POLL_SECONDS (60) so the sweep re-checks it, and the
              job is what moves it on
        v
   job rfp_harvest(email_id)
     1. re-read the row; must be at harvest (a manual run tolerates done/merged/duplicate)
     2. parse the platform reference from body_text (pure)
     3. find or create the rfp_harvests row by external_key
        - complete and young -> link, harvest -> done, stop
     4. CAS the harvest row running; facts first (bid package, bid, bid form),
        written before any download so a later download failure keeps them
     5. documents manifest -> new sandbox run (staging) -> download each file,
        add_upload_file -> start_run + dispatch
     6. harvest complete; email harvest -> done with harvest_id, harvested_at
```

Status vocabulary after this slice: pending gains `harvest`; human and
terminal are unchanged. `STATUS_PENDING` gains `harvest` (the sweep query
and the `_process_email` loop), `set_method` keeps accepting it (it is after
`method`), `dismiss` keeps refusing it.

Waits, all without spending an attempt (the pipeline's box-off pattern):
the harvester is not configured (no credentials) at the moment the step
runs (the row goes to `done`, never waits: a deployment without credentials
is a deployment without the feature); logins are locked; the queue is off.
Real failures spend attempts through `_retry_or_fail` on the email row
(1 min, 5 min, then 15 min; cap `RFP_EMAIL_INGESTION_CLASSIFY_MAX_ATTEMPTS`) and through
the queue's own ladder inside a job.

### 2.1 The step (`_step_harvest`, in `rfp_email_ingest.py`)

1. `rfp_harvest.harvester_for(row)` is None (method has no harvester, or the
   credentials are empty, or `rfp_harvest_enabled` is off): CAS
   `harvest -> done` with `flag_reason` untouched. This is also how a row
   that reached `harvest` under an earlier config drains.
2. `procore_client.availability()` says locked: push `next_attempt_at` by
   the remaining lock, no attempt spent, return.
3. `llm_queue.active_job("rfp_harvest", row.id)` exists: push
   `next_attempt_at` by `RFP_HARVEST_POLL_SECONDS`, return.
4. Otherwise enqueue (`priority = rfp_harvest_queue_priority`, 150: behind
   user-facing LLM work, ahead of the sandbox) and push `next_attempt_at`
   the same way. An enqueue failure goes through `_retry_or_fail`.

The step never calls Procore. It runs inside the sweep like `method`.

### 2.2 The job (`rfp_harvest.execute(email_id, *, force=False)`)

Registered in `llm_queue._spec` as `JOB_RFP_HARVEST = "rfp_harvest"`,
feature `rfp_harvest`, `model_label` "procore", its own `error_message`
(the two service exception classes carry user sentences; anything else
maps to "The harvest was interrupted; it will be retried."), claimed in a
third pass with capacity `rfp_harvest_concurrency - running_harvests`,
skipped while `rfp_harvest_enabled` is false. The job's `current_status`
maps the harvest row's status so the AI monitor's `requeue_terminal`
refuses (`complete`/`failed` -> `done`); the detail screen's "Harvest
again" is the only retry path.

Inside `execute`, in order, every step fenced:

1. Load the email (404 -> `RfpHarvestPermanent`). Pipeline mode requires
   `status = harvest`; manual mode (`force` or a terminal row) never moves
   the status, only `harvest_id` / `harvested_at`.
2. `procore_client.parse_reference(body_text)` -> `ProcoreRef(company_id,
   bid_id, bid_sheet_url)`; None is permanent (`no_platform_link`).
3. Upsert `rfp_harvests (method, external_key)`; on a complete row inside
   the reuse window and not `force`: link and finish (no Procore call).
4. CAS the harvest row `pending|failed|complete -> running` with a fresh
   `claim_token`; a losing CAS (another worker is on the same bid) parks
   this email at `harvest` with a poll wait and returns.
5. Renew the queue lease, then facts: `route_to_bid_sheet` (no redirects;
   its `Location` carries the bid package id; a redirect into
   `login.procore.com` or a 401 means "log in" (section 3.2); a 404 or a
   redirect to a planroom "not found" page is permanent), then the three
   JSON GETs. Write `data`, `raw`, `description_text`, `external_url` on the
   harvest row NOW (`facts_at`), so the facts survive a download failure.
6. Documents: the manifest GET, capped at `rfp_harvest_max_files` rows and
   `rfp_harvest_max_total_bytes` declared bytes (over either cap is
   permanent, recorded with the counts; nothing is downloaded). Create the
   sandbox run (`rfp_ingest.create_harvest_run(rfp_email_id, harvest_id)`,
   `staging`), then per file in manifest order: renew the lease, pace,
   stream the signed URL into a scratch file with a running byte cap
   (`rfp_ingest_max_file_bytes`; over is recorded on the file entry as
   `too_large` and skipped), read it back and `add_upload_file` (sniff,
   sha256, quarantine). The per-file entry in `files` records
   `{file_path, size, kind, discipline, sandbox_file_id, status, error}`.
   A download that fails after 3 paced attempts is recorded as
   `download_failed` and the loop continues; a file the sandbox rejects at
   sniff is recorded from the returned row. Then `start_run` + `dispatch`
   (a run with zero accepted files is deleted; an empty manifest records
   `no_files`, and an all-rejected manifest completes with the per-file
   outcomes as the record).
7. Harvest row `complete` (`finished_at`, counts); email CAS `harvest ->
   done` with `harvest_id`, `harvested_at`, `attempts = 0`, `last_error =
   null`, `next_attempt_at = null`. `flag_reason` is left as the match step
   wrote it (`no_project_name` stays meaningful).

Failure mapping inside the job:

- `ProcoreLoginLocked` / `ProcoreUnavailable` (locked, credentials
  missing at run time, Cloudflare challenge page instead of JSON): the job
  parks the row (harvest row back to `pending` with the sentence, email
  stays at `harvest` with `next_attempt_at` pushed by the lock remainder or
  `RFP_HARVEST_POLL_SECONDS`) and returns normally. No queue attempt is
  spent on a condition a retry cannot fix.
- `httpx.TransportError`, 5xx, 429: `RfpHarvestTransient` -> the queue's
  retry ladder; the harvest row goes back to `pending` with `last_error`.
- `RfpHarvestPermanent` (no link, 403/404 on the bid, caps, manifest shape
  unexpected): harvest row `failed` with the sentence; email `harvest ->
  done` with `harvest_id` set (so the card shows the failure), `last_error`
  set.
- The queue's terminal failure calls `rfp_harvest.mark_from_queue`, which
  does exactly the permanent-failure writes above (CAS-fenced, so a job
  that finished normally is not overwritten).

### 2.3 GC portal scrapers (`gc_portal`, 2026-09-16)

A `gc_portal` invitation (RFP_EMAIL_INGESTION.md 3.8) comes from a GC that
runs its own bidding portal. Every such portal is different, so there is no
one harvester: each GC gets its own scraper, and the scraper is chosen by
the sender's domain.

- `GC_PORTAL_SCRAPERS: dict[str, str]` in `rfp_harvest.py` maps a domain to
  a scraper key, for example `{"gc.example": "gc_example"}`.
  `gc_portal_scraper_for(row)` resolves the email's From domain against it,
  subdomains covered on a label boundary exactly like authorized-sender
  domain rules (`portal.gc.example` resolves, `notgc.example` does not). The
  key is the From domain and not the rule's, so a row whose method was
  corrected by hand still resolves.
- `harvester_for` answers `gc_portal` only when a scraper is registered for
  the sender; otherwise None, and the row drains `harvest -> done` (or
  `match -> done`) unharvested, exactly like organic. `can_harvest` then
  says "No scraper exists yet for this GC's portal (gc.example)." on the
  Project data card.
- The registry is empty today. A registered scraper with no runner never
  walks the Procore path: `execute` fails the harvest visibly ("The
  gc_example scraper is registered for gc.example but nothing runs it yet.")
  and `platform_reference` stays None for the method, so the match exit and
  the step keep draining to done.

Wiring the first scraper: register its domain, give `platform_reference` a
`gc_portal` branch (or replace the link test with the scraper's own "can I
work this email" test) so the match exit routes to `harvest`, and replace
the not-wired branch in `execute` with the scraper call. Facts and files
land the same way Procore's do: an `rfp_harvests` row keyed
`gc_portal:<scraper>:<external id>` and an Ingestion Sandbox run with
`source_kind = rfp_email`.

### 2.4 PipelineSuite (`pipelinesuite`, 2026-09-16)

The second harvester behind `harvester_for`, in full in
`RFP_PIPELINESUITE.md`. A `pipelinesuite` invitation (RFP_EMAIL_INGESTION.md
3.9) comes from a GC whose plan room is a PipelineSuite (PreconSuite)
portal at `<gc>.pipelinesuite.com`; the method is granted by a locked
domain rule on the GC's own domain (0129 seeds `cgandbinc.com` and
`shfcontracting.com`). Unlike `gc_portal`, every such portal is the same
product, so one harvester serves them all and the next GC is a rule.

- `harvester_for` answers `"pipelinesuite"` for the method under
  `RFP_INGESTION_ENABLED`, `RFP_HARVEST_ENABLED` and `PIPELINESUITE_ENABLED`
  alone: no credentials are involved. The portal host, the Project ID and
  the Security Key are parsed from the email body
  (`pipelinesuite_client.parse_reference`); `can_harvest` says so when the
  body carries none (a direct human reply from the same domain).
- Sessions are per portal host: one `rfp_harvest_sessions` row per
  `provider = pipelinesuite:<host>`, the stored `account` a fingerprint of
  the key, never the key. `availability`, the lock, the login thresholds
  and the bell are per portal, and `POST /{id}/harvest` checks the row's
  own portal through `availability_for(row)`. `session_status()` lists
  every portal under a `pipelinesuite` block.
- `_harvest_files` is provider-neutral: it takes any session exposing
  `provider` and `download(url, dest, max_bytes)`, maps each provider's
  `Transient` / `Forbidden` pair the same way, and stamps the sandbox file's
  source kind with the session's provider. Facts first, then caps, then
  files, exactly the Procore order.
- Never answered: the portal's Yes / No / Unsure confirmation, RFI, bid
  upload and logout routes are outside the client's allowlist. The email's
  own open pixel and "View Files" click are fired once per harvest so the
  GC sees the invitation was opened.

### 2.5 Email harvester (`organic`, `general`, `nonorganic`, 2026-09-16)

The third harvester behind `harvester_for`, key `email`
(`HARVESTER_EMAIL`). A GC that invites directly (no platform) sends the
bid documents as email attachments, as cloud-share links (SharePoint and
OneDrive folders, Dropbox folders, Box, Google Drive), or both, so the
"platform" is the email itself. Applies to every row whose method is
`organic`, `general` or `nonorganic`; the harvest row keeps that method
and the external key is the email row: `email:{rfp_email_id}`, so every
email gets its own harvest and "Harvest again" refreshes it.

Decisions (the user, 2026-09-16):

| Topic | Decision |
|---|---|
| Trigger | `email_reference(row)` (pure, `rfp_email_files.py`) answers an `EmailRef` when the stored `attachments_meta` holds at least one file attachment that is not an image and not an attached email, OR `body_text` carries at least one recognised share link (supported or not: an unsupported link still earns the row a harvest so the card can show it as "download by hand"). Otherwise the row drains to `done` as today. `can_harvest` reports "The email carries no attachments or share links to harvest." |
| Attachments | Re-listed live from Graph inside the job (`id, name, contentType, size, isInline, contentId`); the stored meta has no ids. Streamed through `graph_inbox.download_attachment_to_file` under `rfp_ingest_max_file_bytes`. Never downloaded: inline (cid) attachments, images, attached emails (`itemAttachment`, `.msg`, `.eml`). Reference attachments (Outlook cloud "attachments") join the link list through `graph_inbox.list_reference_links`. |
| Images | Every image is skipped from download, inline or not: `image/*` content type OR an image extension (`png jpg jpeg gif bmp tif tiff webp heic svg ico`), because CG&B's PDFs arrive as `application/octet-stream` and the declared type can never decide to KEEP a file (the sandbox sniff does). Signature-like = inline, or named `image\d{3}.*` / `Outlook-*.*`, or at most `rfp_harvest_image_signature_max_bytes` (100 KB). Skipped images go to `data.attachments.images[]` with `signature_like`; the card collapses the signature-like ones to one count and lists the rest by name and size so nothing is silently lost. |
| Zips | Opened (a `.zip` attachment, a zip member of a folder, and the zip Dropbox serves for a folder) by `rfp_zip.py` under hard caps: the end record (ZIP64 honoured) is read before `zipfile` parses the directory and an archive declaring more than `rfp_zip.MAX_ENTRIES` (2000) entries or a central directory over 4 MB is refused outright (`too_many_entries`, so a many-entry archive can never inflate the worker's memory), members counted against `rfp_harvest_file_cap` (members past the kept cap are dropped without being opened for the archive-magic peek), declared uncompressed bytes against `rfp_harvest_max_total_bytes`, each member under `rfp_ingest_max_file_bytes` enforced on the bytes actually inflated, nested zips and encrypted members recorded and never opened, directory entries, `__MACOSX/`, `.DS_Store` and `Thumbs.db` dropped, images skipped by the same policy, every member name reduced to safe path segments (no absolute paths, no `..`). The zip itself is an entry with status `expanded`; each member is an entry whose `file_path` is `<zip name>/<member path>` and whose `zip_of` names the zip. |
| Links | Found in `body_text` and, at harvest time only, in the HTML body (`_email_html`, anchors whose text is not the URL; the HTML is never stored). Safe Links and Google wrappers unwrapped, Outlook `xsdata` / `e=` decoration stripped from the key, capped at `rfp_harvest_link_max_count` (10) resolved links per email, the rest recorded `skipped_cap`. Providers and what "resolve" means are in `cloud_folders.py` (below). Anonymous only, the `cloud_links.py` stance: https, an allowlist of share hosts re-checked on every redirect hop, no app token, no cookies to any host but the share's own. A link that answers with a sign-in wall is recorded `needs_sign_in` with its URL for a person; never retried by the pipeline. Recognised but unsupported hosts (ShareFile, Egnyte, WeTransfer, Hightail, Box folders) are recorded `unsupported` with the URL, same treatment. |
| Reuse | Per link, not per email. Before downloading a link's files the job asks for the accepted files of any `complete` harvest younger than `rfp_harvest_reuse_days` whose `data.links[]` carries the same `key` (`rfp_email_harvest.prior_link_files`: jsonb containment on `data`, the job's own harvest row excluded, newest harvest first so the freshest copy wins, `accepted` entries with a `sandbox_file_id` only); every listed file with the same `file_path` and `size` becomes a `reused` entry (its `sandbox_file_id` and `reused_harvest_id` point at the earlier harvest, nothing is downloaded), so EOC's daily reminders of one folder cost one listing call each and only an addendum's new files are pulled. `force` ("Harvest again") ignores reuse. Attachments are always downloaded (Graph gives no hash). A harvest whose every file was reused creates no sandbox run at all. |
| Facts | There is no platform to ask, so `data` is the email's own file story (section 4) and `description_text` is null. The extract step's project facts already live on the email row. |
| Sessions | None: `session_provider_for` and `availability_for` answer "always usable" for the three methods; no `rfp_harvest_sessions` row, no lock, no bell. |
| Off switch | `RFP_HARVEST_EMAIL_ENABLED` (default true). Off means `harvester_for` answers None for the three methods and rows drain to done as before this slice. |

Flow inside `execute` (`rfp_email_harvest.harvest(sb, settings, email, ref, harvest, token, force=force)`, run under `_run_claimed` with the `cloud_folders` error family `(CloudUnavailable, CloudForbidden, CloudError)`):

1. Attachments: list from Graph (primary mailbox sighting, then any; a copy that 404s falls back to the next, every copy gone is permanent with "The message is no longer in any watched mailbox.", the mailbox not answering (5xx, 429, transport) is transient, any other refusal permanent; the listing is paged through `@odata.nextLink`); classify (`rfp_email_files.classify_attachments`); zip attachments are downloaded now into the job's scratch dir and inspected (`rfp_zip.inspect` under `rfp_harvest_file_cap`, `rfp_harvest_max_total_bytes`, `rfp_ingest_max_file_bytes` and `is_image_name`), their members appended. Attachment entries carry `origin = attachment`, locator `graph:<mailbox>|<message id>|<attachment id>`; members carry `origin = zip`, locator `zip:<scratch path>|<member index>`. A zip that cannot be fetched ends `too_large` / `download_failed`, one that cannot be used ends `rejected` (not a zip, a bomb) or `too_large` (declares more bytes than the harvest accepts) with rfp_zip's sentence, and then no members; an opened zip whose member cap dropped entries keeps the sentence on its `expanded` entry's `error`.
2. Links: `find_share_links(body_text, html)`, plus the reference attachments through `cloud_folders.link_from_url(sourceUrl, name)`, body order first, deduped by key; every link goes through `cloud_folders.resolve(link, scratch, settings)` (an unsupported one is answered without the network and spends none of the resolve cap) and gives a `Listing` (files with relative paths and sizes, plus the folder's status). A Dropbox folder resolves by downloading its zip into scratch and inspecting it (its members are `origin = zip` too, with `link_key` set); the listing carries no image policy, so the harvester applies `is_image_name` to every listed file (images are recorded, never entered) and reads the folder zip a second time with its own predicate for the skipped members. A `.zip` a folder lists is opened like a zip attachment. SharePoint, OneDrive and Google Drive folders are enumerated recursively to `rfp_harvest_folder_max_depth` (4) with the file cap applied while walking. Single-file links list one file. Each link is recorded in `data.links[]` whatever happened (a refusal the resolver raises for one link is that link's `unreachable` record, not the harvest's).
3. Reuse (above), then the facts write: `data`, `files` (every entry with `status = null`, reused ones already `reused`, opened zips already `expanded`), `file_count`, `facts_at`, `external_url`, `description_text` and `instructions_text` null. `_check_caps` over the entries that still need downloading ("The email's files are more than the harvest accepts ({n} of {cap})." / "The email's files are larger than the harvest accepts ({mb} MB of {cap} MB).").
4. `_harvest_files` with an `EmailFileSession` (`provider = "email"`, `download(locator, dest, max_bytes)` dispatching on the locator prefix: `graph:` to `graph_inbox.download_attachment_to_file` (AttachmentTooLarge and AttachmentNotStored become CloudForbidden, 5xx / 429 / transport CloudTransient, any other status CloudForbidden), `zip:` and `https://` to `cloud_folders.download`, which inflates a zip member through `rfp_zip.extract_member`; anything else is refused). Reused and expanded entries are skipped by the loop (`_SETTLED_BEFORE_DOWNLOAD`) and never counted as accepted. The sandbox file's source is `{kind: "email", file_path, harvest_id}`, exactly what `_harvest_files` builds for every provider. Nothing to download means no sandbox run is created.
5. `data.documents` is rewritten from what landed (accepted plus reused), then `_complete_harvest` with "The email carries no files to harvest." as the no-files sentence. A harvest whose only files were reused completes with `sandbox_run_id` null and the reused entries as its record. Scratch is removed and `cloud_folders.forget_jars()` called in `finally`.

`cloud_folders.py` (new; `cloud_links.py` stays as the vendor-reply path and is not changed):

```
ShareLink(url, label, provider, key, kind, supported)
    provider: sharepoint | onedrive | dropbox | gdrive | box | sharefile | egnyte | wetransfer | hightail
    kind: folder | file | unknown
    key: stable per share, e.g. sharepoint:<host>:<share token>, dropbox:<folder id>:<rlkey>,
         gdrive:<id>, box:<shared name>
find_share_links(text: str | None, html: str | None) -> list[ShareLink]   # deduped by key, hard cap 200
Listing(status, files: list[RemoteFile], error: str | None, truncated: bool)
    status: listed | needs_sign_in | unsupported | unreachable | html_page | too_many_files
RemoteFile(path, size, locator)      # locator: an https URL the session may fetch, or zip:<path>|<index>
resolve(link, scratch: Path, settings) -> Listing
download(locator, dest: Path, *, max_bytes: int) -> int        # streaming, O_EXCL dest, per-hop allowlist
CloudError, CloudTransient(CloudError), CloudForbidden(CloudError), CloudUnavailable(CloudError, locked_until=None)
```

Provider notes, from the 2026-09-16 probes: a SharePoint or OneDrive
share URL (`/:f:/s/`, `/:f:/p/`, `/:f:/g/personal/`, `/:b:/`, `/:w:/`,
`/:x:/`, `1drv.ms`) answers a 302 that sets a guest `FedAuth` cookie on
the tenant host and lands on the library view; with that jar,
`/<site path>/_api/web/GetFolderByServerRelativeUrl('<folder>')?$expand=Folders,Files`
lists a folder (the `id=` query of the landing URL is the folder's
server-relative path; the site path is everything up to and including
`/sites/<name>`, `/personal/<name>` or `/teams/<name>`) and
`GetFileByServerRelativeUrl('<file>')/$value` serves a file. A redirect
into `login.microsoftonline.com`, `/_layouts/15/authenticate.aspx`,
`/_layouts/15/accessdenied.aspx` or a 401/403 is `needs_sign_in`. A
Dropbox folder (`/scl/fo/`, `/sh/`) with `dl=1` answers a 302 to
`*.dl.dropboxusercontent.com/zip_download_get/...` (a zip; a password
page is `needs_sign_in`); a Dropbox file (`/scl/fi/`, `/s/`) with `dl=1`
serves the file. Google Drive: a file (`/file/d/<id>`, `open?id=`) via
`drive.google.com/uc?export=download&id=<id>` (the large-file confirm
page is followed once); a folder (`/drive/folders/<id>`) is listed
through the Drive API v3 (`files.list`, `q='<id>' in parents`, fields
`id,name,size,mimeType`, paged) when `GOOGLE_DRIVE_API_KEY` is set, else
through the keyless `drive.google.com/embeddedfolderview?id=<id>#list`
page (entries `flip-entry` with the file id and title; no sizes); a
folder that is neither public nor link-shared answers `needs_sign_in`;
Google-native documents (Docs, Sheets, Slides) are recorded
`unsupported` per file. Box: a file (`/s/<name>` that lands on a file)
via `index.php?rm=box_download_shared_file&shared_name=`; a Box folder
is `unsupported`. ShareFile, Egnyte, WeTransfer and Hightail are
recognised so the card can show the URL, and `unsupported`.

The pure module `rfp_email_files.py` holds the attachment policy
(`is_image_name`, `is_image`, `is_attached_email`, `signature_like`,
`downloadable`, `classify_attachments(meta, settings)`), the trigger
(`email_reference(row)` returning `EmailRef(external_key,
external_url=None, session_provider=None)`), the registry constants
(`HARVESTER_EMAIL`, `EMAIL_METHODS`, `FILE_REUSED`, `FILE_EXPANDED`) and
the entry builders (`attachment_entry`, `zip_member_entry`,
`link_file_entry`), so the policy is tested without Graph and
`rfp_harvest.py` can import it without a cycle. `rfp_email_harvest.py`
holds the job body (`harvest`), the session adapter (`EmailFileSession`)
and the reuse query (`prior_link_files`); it imports `rfp_harvest` for the
shared machinery, so `execute` imports it lazily. `rfp_harvest.py` gains
the `email` key in `harvester_for` / `can_harvest` / `execute`, a
row-based `reference_for(row)` that every former
`platform_reference(method, body_text)` call site uses (`can_harvest`,
`mark_from_queue`, `execute`, `step`, `harvest_for_email`, and
`rfp_email_ingest._park_done`; `platform_reference` itself stays), the
sentence "The email carries no attachments or share links to harvest."
(`_MSG_NO_EMAIL_FILES`), `attachments_meta` and `subject` on
`_EMAIL_SELECT` and `attachments_meta` on the `mark_from_queue` select
(the router's harvest route select carries `attachments_meta`,
`from_address` and `primary_mailbox`; the sweep select already did), the
cloud error classes in `_TRANSIENT_ERRORS` / `_FORBIDDEN_ERRORS` /
`error_message`, and `_EMAIL_ERRORS` for `_run_claimed`. The ingest's
attachment listing stores `inline` (`isInline`) from this slice on; older
rows lack it, and nothing depends on it because the job re-lists live.

Security: the harvester never authenticates anywhere (no Graph app token
on a GC-supplied URL, no stored cookies; the FedAuth guest cookie lives in
the job's memory for one harvest). Every fetched host is on the allowlist
on every hop; every stream is capped on the bytes received, not the
declared size; zip members are inflated under the same cap; scratch is a
per-job `mkdtemp` under `rfp_ingest_scratch_dir`, removed in `finally`.
URLs stored on the row (`data.links[].url`) are the GC's share links,
already https and on a recognised host; the card renders them as
`rel="noopener noreferrer"` external links only for `needs_sign_in` and
`unsupported` links (the download-by-hand cases). No URL ever lands on a
`files[]` entry.

---

## 3. Procore client (`app/services/procore_client.py`)

Pure HTTP; no Supabase import except through the session store callbacks
it is given, so the tests drive it with `httpx.MockTransport`.

### 3.1 Reference parsing (pure)

`parse_reference(body_text) -> ProcoreRef | None`. Outlook rewrites every
link through `*.safelinks.protection.outlook.com/?url=<encoded>`; the
parser unwraps that first (also `urldefense`, harmless if absent), then
reads `app.procore.com` URLs:

| pattern | gives |
|---|---|
| `/{company_id}/company/planroom/route_to_bid_sheet/{bid_id}` | company_id, bid_id (preferred) |
| `/{company_id}/company/planroom/download_zip?bid_id={bid_id}` | company_id, bid_id (fallback) |
| `/{company_id}/company/planroom/bid_packages/{package_id}/bids/{bid_id}` | company_id, package_id, bid_id |
| `/{project_id}/project/public/bid/{bid_id}/intents/...` | bid_id only; the URL itself is never fetched |

Ids are digits only, capped at 12 characters. The bid sheet URL is
rebuilt from the parts, never taken verbatim from the email.
`external_key = f"procore:{company_id}:{bid_id}"`.

### 3.2 Session and login

`ProcoreSession(store)` wraps one `httpx.Client` (HTTP/1.1, 30 s timeout,
`follow_redirects=False`, browser `User-Agent`/`Accept`/`Accept-Language`,
`Referer` on JSON calls set to the bid sheet URL). `store` is the
`rfp_harvest_sessions` adapter: `load() -> dict | None`,
`save(cookies, meta)`, `record_failure(error) -> locked_until`,
`clear_failures()`.

Login flow (captured 2026-09-14), every request paced:

1. `GET https://app.procore.com/auth/procore` with a browser `Accept`
   (this is where the bid sheet route itself bounces an anonymous browser;
   an `application/json` Accept on the route gets a bare 401 instead) ->
   302 `login.procore.com/oauth/authorize?...` -> 302 `login.procore.com/
   ?cookies_enabled=true` -> 200 the email form. The client follows these
   by hand (same-site only: `app.procore.com` and `login.procore.com`,
   refuse anything else; at most 8 hops).
2. Parse `authenticity_token` and the form action
   (`/sessions/submit_login_email`); POST `authenticity_token`,
   `session[email]`, `session[remember_me]=true`; expect 302 to
   `/login/password`.
3. GET it, parse the token and action (`/sessions/submit_login_password`);
   POST `authenticity_token`, `session[password]` (plus every hidden field
   the form carries, so an added field does not break the flow); expect a
   302 back through `oauth/authorize` -> `app.procore.com/auth/procore/
   callback?code=...` -> the route -> 308 to the bid page. Follow by hand,
   same-site only, at most 8 hops.
4. Success is `app.procore.com` `_session_id` present after the callback;
   the caller's single retry of the request that proved the session gone
   is the JSON check. Save the cookie jar (name, value, domain, path,
   expires, secure) and `logged_in_at`; clear failures.
5. Anything else is a `ProcoreLoginFailed` with a sentence that names the
   step (email not accepted, password rejected, unexpected page, Cloudflare
   challenge). The store records it; at `PROCORE_LOGIN_MAX_FAILURES`
   consecutive failures `locked_until = now + PROCORE_LOGIN_LOCK_SECONDS`
   and one bell `rfp_harvest.login_failed` to every IT Admin ("Procore login
   failed N times; harvests are paused until <time>. Check
   PROCORE_LOGIN_EMAIL / PROCORE_LOGIN_PASSWORD."). A successful login
   clears the counter.

Rules: a login is attempted only from `ensure_session()`, which is called
only after a request proved the session gone; never twice within
`PROCORE_LOGIN_MIN_INTERVAL_SECONDS`; never while locked. The password
never appears in a log line, an error message or a stored row. The two
production workers share the stored cookies; the one that logs in saves,
the other picks the jar up on its next `load()` (a stale jar simply fails
one request and reloads before deciding to log in).

`get_json(path)` GETs under `app.procore.com` with the jar; 200 JSON is
returned; 401 or a 302 whose target is `login.procore.com` or
`/auth/procore` raises `ProcoreSessionExpired` (the caller does
`ensure_session()` once and retries once); a 200 with `text/html` where JSON
was expected (a Cloudflare interstitial) raises `ProcoreUnavailable`; 403
and 404 raise `ProcoreForbidden` (permanent); 429 and 5xx raise
`ProcoreTransient`. Every path is checked against `ALLOWED_PATHS` (regexes
for the five endpoints above) before the request is sent; a path outside
it raises `ValueError` and is a test failure, not a runtime branch.

`download(url, dest, max_bytes)` accepts only `https://storage.procore.com/`
URLs, follows exactly one redirect and only to `https://*.amazonaws.com/`,
streams 1 MB chunks under the running cap into `dest` (opened `O_EXCL`),
unlinks on every failure. No cookies are sent to the storage hosts.

### 3.3 Pace

`_pace()` sleeps until at least `min_interval * uniform(1.0, 2.0)` seconds
have passed since the previous Procore request in this process (module
state guarded by a lock: the two claim slots of one worker cannot both
race the pace, and the third claim pass runs one harvest per worker
anyway). The interval applies to logins, JSON calls and downloads alike.

---

## 4. Data model (migration 0123)

`rfp_harvests`

| column | notes |
|---|---|
| id | uuid pk |
| rfp_email_id | uuid not null references rfp_emails on delete cascade; the email whose job created the row (the first one for that bid) |
| method | text not null (`procore` today) |
| external_key | text not null; unique with `method` |
| external_url | text; the rebuilt bid sheet URL |
| status | text check in (`pending`, `running`, `complete`, `failed`) |
| claim_token | text; fences the running writes |
| attempts, last_error | int, text |
| data | jsonb; the normalized facts (below) |
| raw | jsonb; the three API payloads with signed URLs, phone-free contact lists and the recipient list removed |
| description_text, instructions_text | text; HTML stripped |
| files | jsonb array; the manifest with per-file outcome |
| file_count, files_accepted, bytes_downloaded | int, int, bigint |
| sandbox_run_id | uuid references rfp_ingest_runs on delete set null |
| facts_at, started_at, finished_at, created_at, updated_at | timestamptz |

`data` (Procore):

```
{
  "platform": "procore",
  "company_id", "project_id", "bid_package_id", "bid_id", "bid_form_id",
  "project_name", "project_address" (text, lines joined by ", "),
  "project_latitude", "project_longitude",
  "bid_package_title", "bid_package_number",
  "bid_due_at" (ISO instant; date-only platform fields stay "YYYY-MM-DD"), "accept_post_due_submissions",
  "anticipated_award_date", "pre_bid_walk_through_date", "pre_bid_walk_through_notes",
  "pre_bid_meeting_date", "pre_bid_meeting_location", "pre_bid_meeting_online_link",
  "pre_bid_rfi_deadline_date",
  "public_bid_opening_date", "public_bid_opening_location",
  "gc": {"name", "address" (text), "phone", "website"},
  "point_of_contact": {"name", "email", "phone"},
  "distribution_members": [{"name", "email"}],
  "invited_recipients": [emails only],
  "bid_form": {"title", "base_bid_sections": [{"title", "items": [{"description", "unit"?, "quantity"?}]}], "alternates": [...]},
  "accounting_method", "lump_sum_bidding", "require_nda", "blind_bidding",
  "documents": {"count", "bytes", "disciplines": {"Electrical": 7, ...}, "kinds": {"drawing": 28, "specification": 42, "other": 2}}
}
```

`files[]` entries: `{file_path, size, kind (drawing|specification|other),
discipline (from the path), drawing_title, revision, sandbox_file_id,
status (accepted|rejected|too_large|download_failed|skipped_cap), error}`.
Signed URLs are never stored.

`data` (email harvester, 2.5):

```
{
  "platform": "email",
  "attachments": {
    "count": <every attachment Graph listed, whatever its kind>,
    "files": <non-image, non-email file attachments entered into files[]>,
    "images": [{"name", "size", "inline", "signature_like"}],   # attachments, zip members and folder files alike (inline false past the attachments)
    "skipped": [{"name", "size", "reason"}]
        # reason: attached_email | unknown (an attachment kind Graph did not name)
        #         | nested_zip | encrypted | too_large | empty | unsafe_name (zip members, rfp_zip's words);
        #         name is "<zip file_path>/<member path>" for a zip attachment's members, the member path
        #         alone for a Dropbox folder's; a zip that cannot be used is its own entry's
        #         rejected / too_large status, not a skipped row
  },
  "links": [{"key", "provider", "kind" (folder|file|unknown), "url", "label" (null when none),
             "status" (listed|needs_sign_in|unsupported|unreachable|html_page|too_many_files|skipped_cap),
             "file_count", "bytes", "reused", "error"}],   # file_count and bytes count entered files only
  "documents": {"count", "bytes", "kinds": null, "disciplines": null}   # accepted + reused, after the loop
}
```

`files[]` entries for the email harvester add `origin` (attachment | zip |
link), `provider` (the link's provider, or `graph` for an attachment and
for the members of a zip attachment), `link_key` (the `data.links[].key`
the file came from, null for an attachment), `zip_of` (the parent zip
entry's `file_path` for the members of a zip that is itself an entry; null
for the members of a Dropbox folder zip, which has no entry of its own)
and `reused_harvest_id`; `kind`, `discipline`, `drawing_title` and
`revision` are null. `file_path` is the attachment name, `<zip
file_path>/<member path>` for a member, or the path the share reported
(relative to the shared folder, or the file's own name). Two statuses join
the vocabulary for every harvester: `reused` (the file was accepted by an
earlier harvest of the same link; its `sandbox_file_id` is that harvest's)
and `expanded` (a zip that was opened; its members follow it; `error`
carries a sentence when the member cap dropped entries). Locators (Graph
attachment ids, scratch paths, signed or cookie-bound URLs) are never
stored on an entry.

`rfp_harvest_sessions`

| column | notes |
|---|---|
| provider | text pk (`procore`) |
| account | text; the login email at save time (so an env change invalidates the jar) |
| cookies | jsonb |
| logged_in_at, last_used_at, last_verified_at | timestamptz |
| login_failures | int default 0 |
| locked_until | timestamptz |
| last_error | text |
| updated_at | timestamptz |

`rfp_emails`: `harvest_id uuid references rfp_harvests on delete set null`,
`harvested_at timestamptz`; the status CHECK gains `harvest`.

`rfp_ingest_runs`: `source_kind` CHECK gains `rfp_email`; `rfp_email_id
uuid references rfp_emails on delete set null`; `harvest_id uuid references
rfp_harvests on delete set null`. `rfp_ingest_files.manifest.identity.source`
for these files is `{kind: "procore", file_path, harvest_id}`.

`llm_jobs.job_type` CHECK gains `rfp_harvest`.

All new tables: RLS enabled and forced, no policies; `set_updated_at`
trigger; `notify pgrst, 'reload schema'` at the end. Idempotent DDL.

---

## 5. API

Existing router `/rfp-emails` (review-queue roles, feature switch):

| method and path | purpose |
|---|---|
| GET /rfp-emails/{id} | gains `harvest` (the harvest row without `raw`, `claim_token`) and `harvest_job` (queue poll info) |
| POST /rfp-emails/{id}/harvest | manual run: 202 `{job}`; 409 `rfp_harvest_active` when a job is already active; 409 `rfp_harvest_not_available` when the method has no harvester or the email has no platform link; 503 `rfp_harvest_locked` with the unlock time while the logins for the row's own platform are locked (`availability_for(row)`: Procore's one session, or the PipelineSuite portal named in the body). Accepts `{"force": true}` to refresh a complete harvest. Audited `rfp_harvest.run`. Rate limit: the default bucket. |
| GET /rfp-emails/harvest-status | `{enabled, configured, account, logged_in_at, last_used_at, last_login_attempt_at, login_failures, locked_until, last_error, active_jobs}` for the settings tab (manage roles), plus a `pipelinesuite` block (`{enabled, portals: [...]}`, one entry per portal host, section 2.4); never the cookies, never the password, never a Security Key |

The list rows (`GET /rfp-emails?tab=processed`) gain `harvest_status`
(null, pending, running, complete, failed) through the join.

---

## 6. Frontend

- Detail modal, new "Project data" card under the model block: a status
  chip (Waiting for Procore / Harvesting / Complete / Failed with the
  sentence / Not available), the facts grid (project, address, GC, contact,
  bid due, pre-bid walk, RFI deadline, award date), Description and Bidding
  instructions as plain text blocks, the file list (path, size, kind,
  outcome) with counts, and a link "Open in Ingestion Sandbox" to
  `/ingestion-sandbox?run=<id>` when the viewer passes the sandbox gate.
  Button "Harvest project data" (or "Harvest again" when complete), review
  roles only, disabled with the lock sentence while locked. The modal polls
  the detail every 5 s while a harvest job is active.
- Processed tab: a small harvest chip per row.
- Settings, RFP Ingestion tab: a "Procore" block showing configured (the
  account, never the password), last login, lock state, last error.
- Bell: `rfp_harvest.login_failed` routes to `/settings/rfp-ingestion`.
- All strings in the catalogs; no em dashes.

---

## 7. Security notes

- Procore content is attacker-adjacent (a GC types it) and the documents
  are attacker-controlled bytes; they go to the sandbox exactly like an
  emailed attachment, and the facts are stored as text with HTML stripped
  (`nh3` with no tags allowed, then entities unescaped).
- The client can only ever fetch the allowlisted GET endpoints and the two
  login POSTs; the bid intent, submit, sign and email routes are not
  reachable from code. Redirects are followed by hand and only within the
  two Procore hosts (JSON/login) or to the storage host and its one S3 hop
  (downloads).
- The password lives in the env and in the login POST body only. The cookie
  jar is a bearer credential for the account: forced RLS, service role only,
  never returned by any route, replaced on every login, and the `account`
  column ties it to the email so a rotated env drops it.
- Every stored string is capped (`_cap`); the raw payloads are trimmed of
  signed URLs and phone numbers before storage; `files` never holds URLs.
- Rate limits: the manual route uses the default bucket; the job is not
  user-triggered.

---

## 8. Configuration

| env | default | meaning |
|---|---|---|
| RFP_HARVEST_ENABLED | false | this slice; `RFP_INGESTION_ENABLED` still gates everything |
| PROCORE_LOGIN_EMAIL | empty | the Procore account; empty = no Procore harvester |
| PROCORE_LOGIN_PASSWORD | empty | |
| PROCORE_MIN_REQUEST_INTERVAL_SECONDS | 2.0 | pace floor; each gap is this times a random 1.0 to 2.0 |
| PROCORE_LOGIN_MIN_INTERVAL_SECONDS | 600 | never two login attempts closer than this |
| PROCORE_LOGIN_MAX_FAILURES | 3 | consecutive failures before the lock |
| PROCORE_LOGIN_LOCK_SECONDS | 21600 | lock length |
| PROCORE_REQUEST_TIMEOUT_SECONDS | 30 | |
| RFP_HARVEST_CONCURRENCY | 1 | jobs per worker (third claim pass) |
| RFP_HARVEST_QUEUE_PRIORITY | 150 | between user LLM jobs (100) and the sandbox (200) |
| RFP_HARVEST_POLL_SECONDS | 60 | how long a `harvest` row waits between sweep checks |
| RFP_HARVEST_REUSE_DAYS | 14 | a complete harvest younger than this is reused |
| RFP_HARVEST_MAX_FILES | 200 | manifest rows; must be <= rfp_ingest_max_files_per_run |
| RFP_HARVEST_MAX_TOTAL_BYTES | 2 GB | declared manifest bytes |
| RFP_HARVEST_EMAIL_ENABLED | true | the email harvester (2.5) for organic, general and nonorganic rows |
| RFP_HARVEST_LINK_MAX_COUNT | 10 | share links resolved per email; the rest are recorded `skipped_cap` |
| RFP_HARVEST_FOLDER_MAX_DEPTH | 4 | subfolder depth walked in a SharePoint, OneDrive or Google Drive folder |
| RFP_HARVEST_IMAGE_SIGNATURE_MAX_BYTES | 102400 | an image at or under this is signature-like (collapsed on the card) |
| GOOGLE_DRIVE_API_KEY | empty | lists Google Drive folders through the Drive API; empty = the keyless embedded folder view |
| CLOUD_REQUEST_TIMEOUT_SECONDS | 30 | per request to a share host |

Validation: intervals and caps positive (pace floor 0.5 s, login and lock
intervals at least 60 s); `RFP_HARVEST_MAX_FILES` is clamped at use time to
`rfp_ingest_max_files_per_run` (`Settings.rfp_harvest_file_cap`);
`procore_configured` is the derived property (both credentials non-empty).
The `PIPELINESUITE_*` block (on by default, no credentials) is in
`RFP_PIPELINESUITE.md` section 6; `Settings.rfp_harvest_active` is
`rfp_ingest_enabled and rfp_harvest_enabled and (procore_configured or
pipelinesuite_enabled or rfp_harvest_email_enabled)`.

---

## 9. Tests

- `tests/test_procore_client.py` (MockTransport): reference parsing over the
  real captured body (safelinks unwrapped, intent links ignored, ids
  bounded); the login flow end to end with the captured redirect chain,
  including email rejected, password rejected, an interstitial HTML page,
  a redirect off-site (refused); session expiry -> one login -> one retry;
  the lock counter and its bell; the pace (patched clock); the allowlist
  (every endpoint the service calls passes, the intent/submit/sign/email
  routes fail); download host rules and the byte cap.
- `tests/test_rfp_harvest.py`: `normalize_facts` over the captured JSON
  (this document's section 4 shape, HTML stripped, phone numbers absent
  from `raw`); manifest classification (kind, discipline); `execute` in
  pipeline and manual mode against a stub client: reuse inside the window,
  `force`, facts written before downloads, per-file outcomes, caps, run
  created/started/deleted-when-empty, every failure mapping, the losing
  claim CAS, `mark_from_queue` fencing.
- `tests/test_rfp_email_ingest.py`: the match step's two done exits route
  to `harvest` only with a harvester and a link; `_step_harvest` enqueues
  once, waits on an active job, drains to done without credentials;
  `set_method` accepts `harvest`; `dismiss` refuses it.
- `tests/test_llm_queue.py`: the third claim pass and its capacity; the
  spec's `current_status` mapping.
- Router tests: the manual route's four answers; the detail's `harvest`
  block never carries `raw`, cookies or a signed URL.
- `tests/test_rfp_email_files.py` (2.5, pure): the image policy over the
  attachment shapes seen on dev (an octet-stream PDF kept, an octet-stream
  `.png` an image, the declared type never keeps a file), the signature
  test, attached emails, `classify_attachments`, the trigger with
  attachments only, links only, both, neither, images only, an unsupported
  ShareFile link alone, and over the real parser with the probe bodies;
  the entry builders never carry a locator.
- `tests/test_rfp_email_harvest.py` (2.5, the job, against the harvest
  fakes plus recording stand-ins for `cloud_folders`, `rfp_zip` and Graph
  in `tests/fixtures_email_harvest.py`): the registry seams, the selects,
  `step` and `_park_done` routing by the row's files, the pipeline-mode
  run with attachments and a SharePoint folder (the section 4 shapes, no
  locator on the row), the facts before the first download, manual mode,
  the live listing deciding the policy (paged), per-attachment outcomes,
  zip attachments and folder zips (members, skipped members, images, the
  unfetchable / unusable arms), links from the body, the HTML and the
  reference attachments, sign-in walls and unsupported links recorded
  with the URL, the resolve cap, resolver refusals per link, transient
  and unavailable trouble, per-file link download outcomes, reuse (and
  `force`, and a reused-only harvest creating no run), `prior_link_files`,
  the caps over what still needs downloading, the session adapter's error
  mapping, `mark_from_queue` / `harvest_for_email` by key, `error_message`.
  Build record 2026-09-16: 10 + 43 tests, the harvest / ingest / feature
  flag suites kept green (one existing `can_harvest` expectation and the
  router's select pin moved with the seam).
- Live on dev (this session): the Warehouse HVAC Upgrade email, manual
  route, then the pipeline on the next Procore invitation that lands.

---

## 10. Out of scope

- SmartBid and organic (attachment) harvesters: the
  `harvester_for` registry is the seam (PipelineSuite, section 2.4, is the
  second harvester behind it since 2026-09-16). The per-GC portal scrapers
  behind `gc_portal` (section 2.3): the method, the domain-keyed registry
  and the drain-to-done behavior exist; every scraper is its own build.
- Harvesting `merged` / `duplicate` rows (documents for an existing
  project), addendum refresh by manifest diff, and anything that writes to
  Procore or to a PipelineSuite portal. The PipelineSuite All Projects
  sweep and the other PipelineSuite senders seen on dev are listed in
  `RFP_PIPELINESUITE.md` section 9.
- The creation step that turns a harvest into a project.
- Production migration and Railway variables (explicit approval).

---

## 11. Build record and live run (2026-09-15, dev database)

Backend: `app/services/procore_client.py` (pure client), `app/services/
rfp_harvest.py` (facts, manifest classification, session store, job, step),
`rfp_email_ingest.py` (`harvest` in `STATUS_PENDING`, `_park_done` at both
"no existing project" exits of the match step, `_step_harvest`,
`flag_reason` and `harvest_id` in `_SWEEP_SELECT`), `llm_queue.py`
(`JOB_RFP_HARVEST`, `NON_LLM_JOB_TYPES`, third claim pass), `rfp_ingest.py`
(`create_harvest_run`, `add_upload_file(source=)`, the runner keeps a
platform source pointer and reads `manifest->identity->source` with the file
row), `routers/rfp_emails.py` (three routes, `harvest` in the processed tab,
`harvest_status` on list rows, the detail's `harvest` / `harvest_job` /
`harvest_available`), `core/config.py`, `core/error_codes.py`,
`docs/ERROR_CODES.md`, `.env.example`. Tests: `tests/fixtures_procore.py`,
`tests/test_procore_client.py` (85), `tests/test_rfp_harvest.py` (76) plus
additions to the ingest, queue, router and feature-flag suites; 3510 green.

Frontend: `components/RfpHarvestBlock.tsx` (the Project data card),
`components/RfpHarvestStatusSection.tsx` (settings), `lib/rfpEmails.ts`
types, the detail modal button and 5 s poll, the Processed chip, the bell
route, the sandbox `rfp_email` source and its "Open RFP email" link, 78
keys in all six catalogs.

Deviations from the design, all deliberate:

- The login chain starts at the bid sheet route when one is known (the
  route plants Procore's continue-URL cookie; starting at `/auth/procore`
  landed the OAuth callback on the account's company chooser) and stops
  right after the callback: nothing past it is requested.
- Date-only platform fields (award, walk-through, RFI deadline) stay
  `YYYY-MM-DD` in `data`; only timestamps become instants.
- `RFP_HARVEST_MAX_FILES` is clamped at use time to the sandbox's per-run
  cap instead of refusing to boot.

Live run, dev server, real Procore, the user's account, `RFP_HARVEST_ENABLED`
on in the local env:

- Manual path (queue job enqueued for the Warehouse HVAC Upgrade email at
  `done`): one login (email form, password form, OAuth callback, about 30 s
  at the pace), facts written first, 72 of 72 files (109,858,548 bytes, the
  manifest total) downloaded and accepted in about 4.5 minutes, sandbox run
  `e8de2e8b` then verified all 72 (410 pages, 0 rejected). The Project data
  card rendered the facts, description, instructions and file list; the
  settings block showed the account, the login time and 0 failures.
- Pipeline path (the "Replace Housing Units 9 & 10 Door Locks and Controls"
  invitation moved to `harvest` the way `_park_done` writes it, then
  `_process_email`): the step enqueued once and parked the row; the job
  reused the stored session (no second login), harvested 118 files (64
  drawings, 52 specifications, 2 logs; 114.7 MB), and CASed the row
  `harvest -> done` with `harvest_id`, `harvested_at`, `attempts 0`,
  `flag_reason` kept (`no_candidate`). Procore's due date for that bid
  (Sep 21) differs from the email extract (Sep 24): the harvest facts are
  the authoritative ones for the creation slice.
- Cloudflare never interfered with plain `httpx` on either host.

Open items:

- Refreshing a harvest when the manifest changes (addenda) is manual
  ("Harvest again") until a manifest diff is added.
- `raw` strips phone numbers by key only; a phone typed into the HTML
  description survives there (the description is shown as text anyway).
- The Fable subagent hit its usage limit; Opus 5 wrote the test suites.
- Fixed 2026-09-16 (the NGEM review round, shared code): a worker that died
  mid-harvest left the row `running` under its claim token and every later
  attempt parked behind it forever; `_claim_harvest` now also takes a
  `running` row whose `started_at` is older than `LLM_QUEUE_LEASE_SECONDS`
  (`stale_claim_filter`, shared with the NGEM harvest) and a losing claim
  logs a warning. In the same round the retry after an interruption resumes
  past the files the last attempt accepted (`carry_over_accepted`, matched
  by path and size, only when its staging run is reused) and a run that was
  started but never dispatched is dispatched instead of re-downloaded.

---

## 12. Email harvester build record and live run (2026-09-16, dev database)

Built the same day as section 2.5 by three Opus 5 subagents in parallel
(`cloud_folders.py` + `rfp_zip.py`; `rfp_email_files.py` +
`rfp_email_harvest.py` + the seams; the FE card), against the interface
in 2.5. No migration: `rfp_harvests.method` and `external_key` are free
text. Backend suite 4283 green (one pre-existing failure driven by
`RFP_MATCH_AUTO_MERGE_ENABLED=true` in the local env), ruff clean, FE
lint / tsc / build green.

Live run on the dev server (`:5051`, the real mailboxes, four parked
`flagged_unauthorized` rows continued to `nonorganic` through the API,
so extract, match and the harvest step all ran the pipeline path):

- **Dropbox folder** (Pavilion Construction, "Addenda 03 - UMC MLK
  Warehouse Renovation", `3a781fd2`): the folder answered its zip; 14
  members over two folder levels, 14 accepted (54.6 MB) into sandbox run
  `c5454a09`; three inline signature images ignored. One member,
  `Q  A 09-12-26.docx`, was first skipped as `nested_zip` (an office file
  is a zip container); fixed the same hour, office extensions are exempt
  from the magic check (`rfp_zip._DOCUMENT_CONTAINER_EXTENSIONS`).
- **PDF attachment + signature image** (Las Vegas Paving, "RFP - RTC Bus
  Stop Shelter", `16f56c54`): the RFP PDF accepted (699 KB, run
  `c8bf9229`); `image001.jpg` came back from Graph as `isInline: true`
  and was recorded as a signature image, never downloaded.
- **ShareFile** (Slettin, "Re: JOC Child Haven Project", `59c4b19c`):
  complete with zero files, both links recorded `unsupported` with their
  URLs ("ShareFile links must be downloaded by hand."), three signature
  images ignored, `last_error` "The email carries no files to harvest."
- **SharePoint folder** (EOC, "UMC MLK Warehouse Remodel Addendum #3
  Issued", `df5f78e5`): the share redeemed to a guest cookie, the REST
  walk listed the folder and its three subfolders, 16 of 16 PDFs accepted
  (68.5 MB, run `57370c20`), no locator or cookie on any `files[]` entry.
  The same email carried 79 non-inline `IMG_*.HEIC` job-walk photos
  (about 120 MB): all skipped as images and listed by name and size on the
  card (`signature_like = false`), nothing downloaded. Its sibling copy in
  a second mailbox (`f984ff59`) waited behind it as the sibling rule says.

Bugs the live run found and fixed before this record was written:

1. Graph answers HTTP 400 when `contentId` is named in the `$select` of
   the attachments collection (it lives on `fileAttachment` only). The
   listing selects `id,name,contentType,size,isInline`; `isInline` is on
   the base type and is all the policy needs.
2. Office documents inside a zip (above).

Observed, not changed: a dev server on `uvicorn --reload` reloads on every
edit, and the replaced process keeps the sweep lease
(`RFP_EMAIL_INGESTION_LEASE_SECONDS`) until it expires, so rows sit at
`extract` for up to that long after a code change on a dev box. The
manual harvest route (`POST /{id}/harvest`) goes through the queue and is
not affected. A manual "Harvest again" after a pipeline failure links the
new harvest but leaves the email row's own `last_error` from the failed
pipeline attempt in place (the card reads the harvest row's, which is
null); that is the pre-existing manual-mode behaviour for every harvester.
