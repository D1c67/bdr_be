# RFP Ingestion: PipelineSuite harvester (`pipelinesuite`)

Design record for the second platform harvester behind `harvester_for`
(RFP_HARVEST.md). PipelineSuite (PreconSuite, `<gc>.pipelinesuite.com`) is
the bid-invitation product a number of GCs run their own plan room on. The
first two are CG&B Enterprises, Inc. (`cgandbinc.com`,
`cgandbinc.pipelinesuite.com`) and SHF International LLC
(`shfcontracting.com`, `shfcontracting.pipelinesuite.com`). The invitation
email comes from the GC's own domain (through SendGrid), carries the portal
host, a Project ID and a Security Key, and the scraper needs nothing else:
one method-keyed harvester serves every PipelineSuite GC, and the next GC is
a settings row, not code.

Status: design v1, 2026-09-16, written from a live capture of both portals
(the Cimarron scoreboard ITB, CG&B project 377363, rfp_emails `cc6a7ba4`;
the Fire Station 95 ITB, SHF project 377691, rfp_emails `ad34c21e`).
Migration 0129 (dev only until release). Nothing here creates projects,
answers a bid question or sends mail.

Naming, used everywhere: method `pipelinesuite`, pure client
`app/services/pipelinesuite_client.py`, settings prefix `pipelinesuite_`
(env `PIPELINESUITE_`), migration `0129_rfp_pipelinesuite_method.sql`,
`rfp_harvests.method = 'pipelinesuite'` with `external_key =
pipelinesuite:<portal label>:<project id>` (`pipelinesuite:cgandbinc:377363`),
`rfp_harvest_sessions.provider = pipelinesuite:<portal host>`
(`pipelinesuite:cgandbinc.pipelinesuite.com`), sandbox file source kind
`pipelinesuite`, FE label "PipelineSuite" (`rfpEmails.method.pipelinesuite`),
bell `rfp_harvest.login_failed` (shared with Procore, the text names the
portal). `gc_portal` stays as-is for genuinely bespoke portals; its registry
stays empty.

---

## 1. Decisions locked in (2026-09-16, with the user)

| Topic | Decision |
|---|---|
| Method, not a gc_portal scraper | `pipelinesuite` joins `INVITATION_METHODS` (after `procore`) and `RULE_METHODS`. It is granted by a LOCKED domain rule on the GC's own domain (the 0127 mechanism: locked rules outrank the GC's organic match). 0129 seeds `cgandbinc.com` and `shfcontracting.com`. A direct human reply from the same domain gets the method too but carries no portal block, so `can_harvest` says so and the row drains to `done` like any organic row. |
| Trigger | Email-triggered only: the `harvest` step after the match step's "no existing project" exits, plus the manual "Harvest project data" button. No All Projects sweep (the portal lists every invite the key can see; a later slice may use that). |
| Credentials | Parsed from the email body: portal host, Project ID, Security Key. No env credentials. The key is company-wide per portal (identical across every CG&B project, likewise SHF), so one stored session per portal host serves every project. The stored `account` is a fingerprint of the key (sha256 hex, first 16), never the key. |
| Access | Plain `httpx`, no browser. Login is a ColdFusion form POST with no CSRF token; project pages are server-rendered HTML; files download from `opr.pipelinesuite.com` with no cookies at all. No Cloudflare on the portal host; Cloudflare in front of the file host never challenged. |
| Never answer the bid question | The confirmation form on every project page has "Yes" PRE-CHECKED for a project already answered and nothing checked otherwise; a POST to `/ehPipelineSubs/confirmResponse` records an answer. That route, the `.../confirmResponse/<x>` login landing the email's own buttons use, `/submitRFI`, `/uploadBid`, `/dspUpdateInfo`, `/logout`, the zip routes and the PipelineBid links are unreachable from code (section 3.4). Verified 2026-09-16: viewing a project page records nothing (the radio state mirrors the stored answer; the All Projects ledger's Response column did not move). |
| Let them see we opened and clicked | Before touching the portal, the job fires the email's own tracking once, the way a person reading the email would: the SendGrid open pixel (`go.pipelinesuite.com/wf/open?upn=...`, HTML body only) and the "View Files and Project Details" click link (`go.pipelinesuite.com/ls/click?upn=...`), each a single GET with NO redirect following (the click counts on that hit; its 302 lands on a per-contact auto-login route we never use). The Yes / No / Unsure links are click links too and are never requested: the click link is chosen by its anchor text. Outcome recorded in `data.tracking`; a ping failure never fails the harvest. |
| Addenda for known projects | Not pulled. An amendment/addendum email about an existing project matches and goes `merged`/`exists`; merged rows are not harvested (same open item as Procore). |
| Files | Every file in the project's file tree (folders walked), downloaded one at a time from `opr.pipelinesuite.com` and handed to the Ingestion Sandbox as one run (`source_kind = rfp_email`) through `add_upload_file`, so PDFs are verified and Office files go through the 0125 conversion. Sizes on the page are KB. Same caps as Procore (`rfp_harvest_file_cap`, `rfp_harvest_max_total_bytes`, per-file `rfp_ingest_max_file_bytes`). |
| Dedup, failure, who sees it | Exactly as RFP_HARVEST.md section 1: one `rfp_harvests` row per `external_key`, reused for `RFP_HARVEST_REUSE_DAYS`; transient waits/retries, permanent fails visibly and the email moves on; review roles see the card. |
| Terms | The user's call 2026-09-16: no note to PreconSuite; proceed as with Procore. |

---

## 2. The email (captured facts)

Plain text as stored in `rfp_emails.body_text` (Outlook rewrites every link
through `nam09.safelinks.protection.outlook.com/?url=<encoded>`; the
platform's own tracker is `go.pipelinesuite.com/ls/click?upn=...`):

```
View Files and Project Details <https://nam09.safelinks.../?url=http%3A%2F%2Fgo.pipelinesuite.com%2Fls%2Fclick%3Fupn%3D...>
Note: If you are unable to click on the above link, you can login at HTTPS://CGANDBINC.PIPELINESUITE.COM
Project ID:     377363
Security Key:   aB1cD2eF@ (example)
[Powered By PreconSuite]<...>
```

Rules for `parse_reference(body_text) -> PipelineSuiteRef | None` (pure):

- host: first `https?://([a-z0-9-]+\.pipelinesuite\.com)` match, case-insensitive
  (the email prints it upper-case), lowercased, whose first label is not
  `go`, `opr`, `cdn` or `www`. Label must match `^[a-z0-9-]{1,63}$`.
- project_id: `Project ID:\s*(\d{1,12})`.
- security_key: `Security Key:\s*(\S{1,64})` (keys carry `!`, `@`, mixed case;
  the whitespace-delimited token is taken as-is).
- All three required; otherwise None. The reference never stores the click
  link (that is read from the HTML at run time).

`PipelineSuiteRef(host, project_id, security_key)` (frozen dataclass; its
`repr` and `str` never include the key):

| property | value |
|---|---|
| `label` | first host label (`cgandbinc`) |
| `external_key` | `pipelinesuite:{label}:{project_id}` |
| `session_provider` | `pipelinesuite:{host}` |
| `project_url` | `https://{host}/ehPipelineSubs/dspProject/projectID/{project_id}` |
| `next_token` | urlsafe base64 of `ehPipelineSubs/dspProject/projectID/{project_id}`, no padding |
| `login_page_url` | `https://{host}/general/index/next/{next_token}` |
| `login_post_url` | `https://{host}/ehPipelineSubs/login/` |
| `external_url` | alias of `project_url` (what `rfp_harvests.external_url` stores) |

`ProcoreRef` gains the same `external_url` alias (of `bid_sheet_url`) so
`_find_or_create_harvest` reads one name.

The email HTML (fetched from Graph at run time, section 4 step 3) carries:

- the open pixel: `<img src="http://go.pipelinesuite.com/wf/open?upn=...">`;
- the click links: `<a href="<safelinks or bare>go.pipelinesuite.com/ls/click?upn=...">View Files and Project Details</a>`,
  and the same shape for `Yes`, `No`, `Unsure` (twice each in the ITB).

`parse_tracking(html) -> Tracking(open_url | None, click_url | None)`:
the pixel is the first `img` whose unwrapped `src` host is
`go.pipelinesuite.com` and path `/wf/open`; the click link is the first
anchor whose visible text, tags stripped and whitespace collapsed, contains
`view files` (case-insensitive) and whose unwrapped href host is
`go.pipelinesuite.com` with path `/ls/click`. An anchor whose text is
`yes`, `no` or `unsure` is never returned (asserted by test). Unwrapping =
`procore_client.unwrap_link` (safelinks and urldefense).

---

## 3. PipelineSuite client (`app/services/pipelinesuite_client.py`)

Pure HTTP, no Supabase import; the session store is injected (the same
`SessionStore` protocol Procore uses: `load`, `save_cookies(account,
cookies)`, `record_login(ok, error) -> state`, `touch`). Tests drive it with
`httpx.MockTransport` over the captured pages (fixtures in
`tests/fixtures_pipelinesuite.py`, with the key, the `cne`/`c` hidden values
and every `upn=` token replaced by dummies).

### 3.1 Portal facts (captured 2026-09-16)

- Stack: ColdFusion (`CFID`, `CFTOKEN`, `JSESSIONID=...cfusion` cookies,
  `AWSALB`/`AWSALBCORS` from the load balancer), Apache, v5.7.97. HTTP/1.1.
- Anonymous GET of any `/ehPipelineSubs/...` route -> 302 to
  `/general/index/next/<urlsafe b64 of the route>`. That page holds
  `<form method="post" action="/ehPipelineSubs/login/" name="portalLogin">`
  with `next` (hidden), `portalProjectID`, `portalSecurityKey`.
- Login POST -> 302 to `https://{host}/{decoded next}` on success; 302 to
  `https://{host}/general/index` on a bad key. The session then reaches
  EVERY project the key was invited to (`/ehPipelineSubs/dspAllProjects`
  lists them: not requested by this slice).
- Project page `/ehPipelineSubs/dspProject/projectID/{id}` (200, HTML):
  - brand/GC name in the header nav (`CG&B Inc.`, `SHF International LLC`)
    and the project title in `<h2>`/page heading;
  - `#viewRespond > #bidding > #confirmation form[action=/ehPipelineSubs/confirmResponse]`
    with hidden `cne` (contact id), `enc` (project id), `c` (contact token),
    `projectView`, `updateTrades`, the invited-name paragraph
    ("Thomas Moore with G3 Electrical Technologies:"), and one
    `.invitations .trade` per trade: `.tradeName` = `<strong>26000</strong> (Electrical : Electrical )`,
    radios `confirmed<tradeId>` values 1/2/3;
  - `#files form#downloadFiles` (POST = Download Selected, never used) and
    `.opr_files ul` = a jstree: `li.folder[data-folder-id][data-text]` with
    a nested `ul`, and `li.file[data-file-path="opr.pipelinesuite.com/<client>/<project>/<name.ext>"][data-file-id][data-text][data-file-size=<KB>][data-uploaded-on=M/D/YYYY]`.
    `data-text` may lack the extension (SHF); `data-file-path` never does.
    File names carry spaces and sometimes a trailing space before `.pdf`.
    The "Download All Files" link is `.../dspProject/projectID/<id>/allFiles/1` (zip, never used);
  - `#projectInfo table#main` label/value rows: `Project #`, `Project Name`,
    `Location`, `Address`, `City`, `State`, `Zip`, `Bid Date`
    (`September 22, 2026`), `Bid Time` (`1:00 PM`), `Scope` (multi-line),
    `Plans`, `Other Info`; then, when amendments exist, a Notices table
    (`Title`, `Created By`, `Created On`);
  - `#projectContacts` (when present): `Company`, `Contact`, `Title`, `Phone`,
    `Extension`, `Fax`, `Email`;
  - `#rfi form[action=/ehPipelineSubs/submitRFI]` and
    `#bid-upload form[action=/ehPipelineSubs/uploadBid]` (never used);
  - a PipelineBid upsell (`pipelinebid.com/register/projectID/<id>/securityKey/<key>`: never followed).
- Files: `GET https://opr.pipelinesuite.com/<client>/<project>/<name>`
  (path segments percent-encoded), Cloudflare, PHP, no cookies, `200
  application/pdf` (or the Office MIME) with `content-length`; a miss is
  `404 text/html`.
- Tracking: `GET http://go.pipelinesuite.com/wf/open?upn=...` -> `200
  image/gif` (43 bytes); `GET http://go.pipelinesuite.com/ls/click?upn=...`
  -> `302` to `https://{host}/ehPipelineSubs/login/enc/<project>/cne/<contact>/c/<token>`
  (the auto-login the email buttons use; never requested).

### 3.2 Session and login

`PipelineSuiteSession(config, store, ref, on_lock)` wraps one
`httpx.Client` (HTTP/1.1, `follow_redirects=False`, timeout
`pipelinesuite_request_timeout_seconds`, browser `User-Agent` / `Accept` /
`Accept-Language`). `store` is the `rfp_harvest_sessions` adapter for
`provider = ref.session_provider`; `config.account` is the key fingerprint.
A stored jar whose `account` differs from the fingerprint is ignored (the
key rotated). Class attribute `provider = "pipelinesuite"` (the sandbox
source kind).

`get_project_page() -> str` (HTML): GET `ref.project_url` with the jar,
paced. 200 whose body contains `id="projectInfo"` -> return it. 302 whose
`Location` path starts with `/general/index` (or a 200 that is the login
page) -> `PipelineSuiteSessionExpired`; the caller does `ensure_session()`
once and retries once. 404 -> `PipelineSuiteForbidden` ("The portal has no
project {id} for this Security Key."). 429 / 5xx / transport ->
`PipelineSuiteTransient`. Any other 200 -> `PipelineSuiteUnavailable`
("The portal answered with a page the harvest does not understand.").

`ensure_session()`: refuse while locked (`PipelineSuiteLoginLocked`) or
within `pipelinesuite_login_min_interval_seconds` of the last attempt
(`PipelineSuiteUnavailable`, waits). Then: GET `ref.login_page_url`
(cookies are planted here; must contain the `portalLogin` form or it is a
`PipelineSuiteLoginFailed("unexpected page")`); POST `ref.login_post_url`
with `next=ref.next_token`, `portalProjectID`, `portalSecurityKey` plus
every hidden field the form carried, `Referer` = the login page, `Origin` =
the portal origin. Success = 302 whose `Location` path is exactly
`/ehPipelineSubs/dspProject/projectID/{id}`; a 302 to `/general/index` is
`PipelineSuiteLoginFailed("The portal rejected the Project ID and Security
Key.")`; anything else names the step. On success save the jar (name,
value, domain, path, expires, secure) with the fingerprint and clear
failures; on failure `record_login(ok=False)`, and when the returned state
carries `locked_until`, call `on_lock(until, error, host)` once. The key
never appears in a log line, an exception message, a stored row or a
fixture.

`download(url, dest, max_bytes) -> int`: accepts only
`https://opr.pipelinesuite.com/<digits>/<digits>/<one path segment>` (the
URL is built by the client from the parsed `data-file-path`, never taken
from anywhere else), a separate cookie-less client, no redirects, 1 MB
chunks under the running cap into `dest` (`O_EXCL`), unlinked on every
failure. 404, or a 200 with a `text/html` content type, ->
`PipelineSuiteForbidden`; over the cap -> `PipelineSuiteForbidden("larger
...")` (so the service's existing `"larger"` test maps it to `too_large`);
429 / 5xx / transport -> `PipelineSuiteTransient`.

`ping(url) -> int | None`: GET with no redirects, 10 s timeout, only when
the unwrapped host is `go.pipelinesuite.com` and the path is `/wf/open` or
`/ls/click` (anything else raises `ValueError`); returns the status code, or
None on any transport error. Paced like every other request.

Exceptions: `PipelineSuiteError` base, `PipelineSuiteTransient`,
`PipelineSuiteUnavailable(locked_until=None)`, `PipelineSuiteLoginLocked`
(subclass of Unavailable), `PipelineSuiteLoginFailed(step, message)`,
`PipelineSuiteSessionExpired`, `PipelineSuiteForbidden`. Same
`llm_error_kind` markers as Procore's.

### 3.3 Pace

Module-level `_pace()` exactly like `procore_client._pace` (its own lock and
clock, `pipelinesuite_min_request_interval_seconds` x U(1, 2)), applied to
the login GET/POST, the project page, every download and every ping.

### 3.4 Allowlist and deny list (code, test-asserted)

Allowed, and the only requests the client can send:

| method | URL |
|---|---|
| GET | `https://<label>.pipelinesuite.com/general/index/next/<token>` |
| POST | `https://<label>.pipelinesuite.com/ehPipelineSubs/login/` |
| GET | `https://<label>.pipelinesuite.com/ehPipelineSubs/dspProject/projectID/<digits>` (nothing after the id) |
| GET | `https://opr.pipelinesuite.com/<digits>/<digits>/<segment>` |
| GET | `http(s)://go.pipelinesuite.com/wf/open?upn=...` and `/ls/click?upn=...` (no redirects) |

`assert_allowed(method, url)` runs before every request; a violation is a
`ValueError` (a test failure, never a runtime branch). A test feeds every
URL on the project page and in the email through it and asserts that
`confirmResponse`, `login/enc/`, `submitRFI`, `uploadBid`, `dspUpdateInfo`,
`logout`, `allFiles`, `dspAllProjects`, `pipelinebid.com`, the safelinks
host and the Yes/No/Unsure click links are all refused.

### 3.5 Page parsing (pure)

`parse_project_page(html) -> ProjectPage`:

```
ProjectPage(
  logged_in: bool,                       # has #projectInfo
  gc_name: str | None,                   # header brand
  title: str | None,                     # page heading
  invited_name: str | None,              # "Thomas Moore with G3 Electrical Technologies"
  response_recorded: bool,               # any confirmation radio checked (informational)
  trades: [{"code": "26000", "name": "Electrical"}],
  info: {label_snake: text},             # project_number, project_name, location, address, city, state, zip, bid_date, bid_time, scope, plans, other_info (text, HTML stripped, entities unescaped, whitespace collapsed except newlines in scope)
  notices: [{"title", "created_by", "created_on"}],
  contacts: [{"company", "name", "title", "phone", "extension", "fax", "email"}],
  files: [{"file_id", "name", "file_path", "folder", "size_kb", "uploaded_on", "url"}],
)
```

`files` walks nested folders depth-first in page order; `folder` is the
folder names joined with `/` (empty at the root); `name` is the basename of
`data-file-path` (extension kept, trailing spaces before the extension
squeezed); `url` is `https://` + `data-file-path` with each path segment
percent-encoded. `bid_due_at(info) -> str | None` combines `bid_date` and
`bid_time` in `America/Los_Angeles` into an ISO instant, or the `YYYY-MM-DD`
day when the time is missing/unparseable, or None.

`classify_name(name) -> kind`: `drawing` when the name matches
`\b(dwg|drawings?|plans?|sheets?|bid set|compiled set)\b` (case-insensitive)
or the extension is `dwg`; `specification` when it matches
`\b(spec|specs|specifications?|manual|addend\w*|amend\w*|itb|ifb|rfp|scope)\b`;
otherwise `other`. Discipline is always None.

---

## 4. Service wiring (`rfp_harvest.py`)

Vocabulary (`rfp_email_auth.py`): `METHOD_PIPELINESUITE = "pipelinesuite"`,
in `INVITATION_METHODS` after `procore` and in `RULE_METHODS` after
`procore`. The router's method lists and `MethodPatchIn` /
`AuthorizedSenderIn` literals gain it.

- `harvester_for(row)`: `method == pipelinesuite and s.rfp_ingest_enabled
  and s.rfp_harvest_enabled and s.pipelinesuite_enabled` -> `"pipelinesuite"`.
  No credentials are involved.
- `platform_reference(method, body_text)`: `pipelinesuite` ->
  `pipelinesuite_client.parse_reference`.
- `can_harvest(row)`: `pipelinesuite` off -> `_MSG_NO_HARVESTER`; no
  reference -> "The email carries no PipelineSuite Project ID and Security
  Key."; else ok.
- `availability(settings, provider=pc.PROVIDER)` and the session store take
  a `provider` (and the matching failure/lock thresholds). `step()` and the
  router's lock check ask for the row's provider
  (`session_provider_for(row) -> str | None`: `procore` or
  `pipelinesuite:<host>` from the parsed reference).
- `session_status()` gains `"pipelinesuite": {"enabled": bool, "portals":
  [{host, account, logged_in_at, last_used_at, last_login_attempt_at,
  login_failures, locked_until, last_error}]}` from every
  `rfp_harvest_sessions` row whose provider starts with `pipelinesuite:`.
- `_notify_lock(until, error, portal=None)`: the text names the portal:
  "PipelineSuite login (cgandbinc.pipelinesuite.com) failed repeatedly; RFP
  harvests for that portal are paused until <time>. The Project ID and
  Security Key come from the invitation email." Bell dedupe unchanged.
- `_harvest_files` becomes provider-neutral: it takes any session exposing
  `provider` and `download(url, dest, max_bytes)`, catches the Procore and
  PipelineSuite `Transient` / `Forbidden` pairs, and writes
  `source = {"kind": session.provider, "file_path", "harvest_id"}`. The
  filename handed to `add_upload_file` is the entry's basename (extension
  kept). `_download_one` likewise.

`execute()` gains a `pipelinesuite` branch after the `gc_portal` branch,
sharing `_find_or_create_harvest`, `_reusable`, `_claim_harvest`,
`_update_claimed`, `_harvest_files`, `_permanent`, `_done`, `_release`:

1. `ref = platform_reference(...)`; None -> permanent (the sentence above).
2. Find or create the harvest row (`external_url = ref.project_url`); reuse
   inside the window unless `force`; CAS-claim exactly as Procore.
3. Tracking pings (skipped when `pipelinesuite_tracking_pings_enabled` is
   off or the existing harvest row's `data.tracking` already has
   `pinged_at`): fetch the email HTML through Graph (`graph_inbox.get_message
   (graph_message_id, mailbox=..., select="id,body", body_type="html")`
   using the `rfp_email_sightings` row for the email's `primary_mailbox`,
   else any sighting); `parse_tracking`; `ping(open_url)` then
   `ping(click_url)`. Graph or parse failure -> fall back to the click link
   parsed from `body_text` (the `View Files and Project Details <url>` line)
   and no open ping. Record `data.tracking = {"pinged_at", "opened":
   bool | None, "clicked": bool | None, "error": str | None}` (bool = the
   ping returned 200/302). Nothing here can raise out of the job.
4. `with open_pipelinesuite_session(settings, ref) as session`: availability
   (per-portal lock -> park, no attempt spent); `get_project_page()` (one
   `ensure_session` + retry on expiry); `parse_project_page`; not
   `logged_in` after the retry -> `PipelineSuiteUnavailable`.
5. Facts first (`facts_at`), then caps, then files, then complete: exactly
   the Procore order, so a download failure keeps the facts.

`data` (pipelinesuite):

```
{
  "platform": "pipelinesuite",
  "portal_host", "portal_label", "project_id",
  "project_number", "project_name",
  "project_address" (address, city, state zip joined ", "; location appended when it adds information),
  "location",
  "bid_due_at" (ISO instant, or "YYYY-MM-DD"), "bid_date_text", "bid_time_text",
  "gc": {"name": <header brand>, "address": null, "phone": <first contact phone>, "website": null},
  "point_of_contact": {"name", "email", "phone"}   (the first Project Contact, else the RFI contact named in the scope when an email address appears there, else null),
  "contacts": [...],
  "invited_name", "trades": [{"code", "name"}],
  "notices": [{"title", "created_by", "created_on"}],
  "other_info", "plans",
  "response_recorded": bool,
  "tracking": {...},
  "documents": {"count", "bytes", "kinds": {...}, "folders": [...]}
}
```

`description_text` = the scope (the leading "CLICK YES / NO / UNSURE"
banner line stripped); `instructions_text` = `other_info` and `plans` when
present, joined by a blank line. `raw` = `{project_info, trades, notices,
contacts, files head}`; no URL, no key, no `cne`/`c` token. `files[]`
entries: `{file_path: "<folder>/<name>" (or "<name>"), size: bytes
(KB x 1024), kind, discipline: null, file_id, uploaded_on, sandbox_file_id,
status, error}`; the download URL list is kept in memory only.

A test asserts the Security Key string appears nowhere in the harvest row,
the email row's updates, the queue row or any log record produced by
`execute`.

---

## 5. Migration 0129 (`0129_rfp_pipelinesuite_method.sql`)

Apply after 0128. Idempotent, the 0127/0128 constraint pattern (look the
check up by column, drop, re-add named):

- `rfp_emails_invitation_method_check`: `organic, procore, pipelinesuite,
  gc_portal, general, nonorganic`.
- `rfp_authorized_senders_method_check`: `procore, pipelinesuite, gc_portal,
  general`.
- Seed two locked domain rules, `on conflict (kind, value) do update set
  method = 'pipelinesuite', locked = true`:
  `('domain', 'cgandbinc.com', 'pipelinesuite', true)`,
  `('domain', 'shfcontracting.com', 'pipelinesuite', true)`.
- `notify pgrst, 'reload schema'`.

`rfp_harvests.method` and `rfp_harvest_sessions.provider` have no check
constraints (0123); no DDL for them.

Release step after applying: rows from those senders already parked at
`flagged_unauthorized` are re-authorized the same way the POST
authorized-senders route rescans (call that service function once per
seeded domain); the migration itself touches no rows.

---

## 6. Configuration

| env | default | meaning |
|---|---|---|
| PIPELINESUITE_ENABLED | true | the harvester; `RFP_INGESTION_ENABLED` and `RFP_HARVEST_ENABLED` still gate it |
| PIPELINESUITE_TRACKING_PINGS_ENABLED | true | fire the open pixel and the View Files click once per harvest |
| PIPELINESUITE_MIN_REQUEST_INTERVAL_SECONDS | 2.0 | pace floor, x U(1, 2) |
| PIPELINESUITE_LOGIN_MIN_INTERVAL_SECONDS | 600 | per portal |
| PIPELINESUITE_LOGIN_MAX_FAILURES | 3 | per portal |
| PIPELINESUITE_LOGIN_LOCK_SECONDS | 21600 | per portal |
| PIPELINESUITE_REQUEST_TIMEOUT_SECONDS | 30 | |

Validation mirrors `procore_*`. `Settings.rfp_harvest_active` becomes
`rfp_ingest_enabled and rfp_harvest_enabled and (procore_configured or
pipelinesuite_enabled)`. `.env.example` documents the block.

---

## 7. API and frontend

- `GET /rfp-emails/{id}` unchanged in shape; `harvest.data.platform` says
  which grid to render. `harvest_available.reason` carries the new
  sentences. `POST /{id}/harvest` answers 503 `rfp_harvest_locked` from the
  row's own portal lock.
- `GET /rfp-emails/harvest-status` gains the `pipelinesuite` block.
- Authorized senders: `method` accepts `pipelinesuite` (locked only, like
  `procore` and `gc_portal`).
- FE `lib/rfpEmails.ts`: `InvitationMethod` and the rule method union gain
  `"pipelinesuite"`; `RfpPipelineSuiteHarvestData` typed as section 4;
  `RfpHarvestStatus.pipelinesuite`.
- `RfpAuthorizedSendersSection.tsx`: picker option "PipelineSuite".
- `RfpHarvestBlock.tsx`: `isPipelineSuiteData` -> `PipelineSuiteFactsGrid`:
  Project name, Project #, Address (+ location), Bid due, GC, Contact
  (name, title, phone, email), Trades invited, Notices (title, by, on),
  Other info, Email signals ("Opened and clicked", "Clicked", "Not sent",
  with the time). Description block shows the scope; instructions block
  the other info. File rows come through `normalizeFiles` unchanged
  (`file_path` entries).
- `RfpHarvestStatusSection.tsx`: a "PipelineSuite" block listing each
  portal (host, last login, failures, lock, last error). No account, no key.
- Processed tab chip and every method label: `rfpEmails.method.pipelinesuite`
  = "PipelineSuite" in all six catalogs (the namespace is English in every
  catalog). No em dashes anywhere.

---

## 8. Tests

- `tests/fixtures_pipelinesuite.py`: the captured CG&B project page (flat
  files), the SHF project page (a folder, `.docx` files, extension-less
  `data-text`, a trailing space before `.pdf`), the login page with and
  without `next`, the two email bodies (text) and the HTML with the pixel
  and the click links; every key / token / `upn` replaced by dummies.
- `tests/test_pipelinesuite_client.py` (MockTransport): reference parsing
  (upper-case host, the tracker/asset hosts skipped, key with punctuation,
  each missing piece -> None, `repr` without the key); the login chain
  (success, bad key, unexpected page, lock after N failures with one bell,
  min interval); session expiry -> one login -> one retry; project page
  parsing over both fixtures (info, trades, notices, contacts, files with
  folders and encoded URLs, `bid_due_at` in Pacific); tracking parsing
  (pixel found, View Files found, Yes/No/Unsure never returned, safelinks
  unwrapped); the allow/deny list over every URL in both pages and the
  email; download host rules, the cap, 404 and HTML-as-file; pace.
- `tests/test_rfp_harvest.py` additions: `harvester_for` /
  `platform_reference` / `can_harvest` for the method; `execute` in
  pipeline and manual mode against a stub session and a stub Graph: pings
  fired once and recorded, skipped on a later run, never fatal; facts before
  files; KB -> bytes; caps; per-file outcomes including a `.docx`; every
  failure mapping; the per-portal lock parks without an attempt; the key
  never persisted or logged.
- `tests/test_rfp_email_auth.py`, `test_rfp_emails_router.py`: vocabulary,
  the locked rule with method `pipelinesuite`, `MethodPatchIn`, the
  migration file test in the 0127 style (both value sets, the two seeds,
  the `on conflict` clause, the `notify`).
- FE: `next lint` and `next build` clean.

---

## 9. Out of scope

- The All Projects sweep (every invite the key can see) and any
  addenda/notice refresh for known projects.
- The other PipelineSuite senders seen on dev (`amrize.com`, `amesco.com`):
  a settings row each, when the user wants them.
- Production migration and Railway variables (explicit approval).
