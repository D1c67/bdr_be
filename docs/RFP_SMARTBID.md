# RFP Ingestion: SmartBid harvester (`smartbid`)

Design record for the third platform harvester behind `harvester_for`
(RFP_HARVEST.md), built the PipelineSuite way (RFP_PIPELINESUITE.md).
SmartBid (ConstructConnect's SmartBid, sometimes "SmartInsight") is the
bid-invitation product a number of GCs send through. Every invitation comes
from the platform's own address (`notifications@com2.smartbidnet.com`), not
from the GC, so like Procore the method is granted by one LOCKED domain rule
on the platform domain, and one method-keyed harvester serves every GC that
uses it. Seen on dev 2026-09-08 to 10-01: DC Building Group, R&O
Construction, Martin-Harris Construction, and others, about 60 rows, all
parked at `flagged_unauthorized` until this method existed.

Status: design v1, 2026-10-01, written from a live capture (the NSU Gateway
Information for Bidders notice, DC Building Group bid project 874974,
rfp_emails `d6f1c05c`; the UNLV Dental ITB, Martin-Harris 876398,
rfp_emails `3558a058`; plus 874512 R&O and 876488 Martin-Harris read only).
Migration 0148 (dev only until release). Nothing here creates projects,
answers a bid question, accepts an agreement or sends mail.

Naming, used everywhere: method `smartbid`, pure client
`app/services/smartbid_client.py`, settings prefix `smartbid_` (env
`SMARTBID_`), migration `0148_rfp_smartbid_method.sql`,
`rfp_harvests.method = 'smartbid'` with `external_key = smartbid:<bid project
id>` (`smartbid:874974`), `rfp_harvest_sessions.provider = smartbid` (one
row: login bookkeeping only, nothing secret stored), sandbox file source
kind `smartbid`, FE label "SmartBid" (`rfpEmails.method.smartbid`), bell
`rfp_harvest.login_failed` (shared; the text names SmartBid).

---

## 1. Decisions (2026-10-01, the user: "build it just like pipeline suite")

| Topic | Decision |
|---|---|
| Method | `smartbid` joins `INVITATION_METHODS` and `RULE_METHODS` right after `pipelinesuite`. Granted by a LOCKED domain rule on `smartbidnet.com` (covers `com2.smartbidnet.com` on the label boundary, like `procoretech.com`). 0148 seeds it. The sender is the platform, so the GC comes from the extract step (the email signature) exactly as for Procore. |
| Trigger | Email-triggered only, the PipelineSuite way: the `harvest` step after the match step's "no existing project" exits, plus the manual "Harvest project data" button. No project-list sweep. |
| Credentials | Parsed from the email body: the "Click Here to View the Project" link carries `cId=bp_<comm detail id>`, `sPassportKey=<40 hex>` and `sBidId=<bid project id>`. The passport key is per recipient contact AND per project (identical across every notice for that project to that address; Bids@, office@ and tmoore@ each get their own). No env credentials. Nothing secret is stored: the bearer token is used for one harvest and dropped. |
| Access | Plain `httpx`, no browser. The portal is an Ember SPA (`gocc.smartbid.co`) over a JSON API (`apicc.smartinsight.co`); the harvester talks to the API only. Files: a per-file security token, then a direct-URL lookup, then an Azure blob GET (section 3.1). |
| Never answer the bid question | The email's "Yes, I'll Bid All Codes" / "No, I Won't Bid this Job" links are the same `Main/Login.aspx` link plus `iR=1` / `iR=0`; the SPA then calls `POST /api/projects/setallcodesanswer` or `GET /api/projects/linkwontbidthisjob`. Every `iR` link, both API calls and everything else not on the allowlist (section 3.4) is unreachable from code. Viewing the project records nothing about intent (captured: 874974 stayed "Accepted" as a human set it, 876398 stayed "Invited"). |
| Never accept an agreement | `getconfidentialagreement` is read first. A project that needs a confidentiality agreement (general or specific) the contact has not accepted, a PAD invitation, or any `AlowedDetail` text fails permanently with a sentence telling a human to open it in SmartBid. Folder or file entries flagged `SCARequired` / `PQRequired` are skipped per file. Nothing is ever accepted or posted. |
| Let them see we opened and clicked | As PipelineSuite decision 4: before touching the API the job fires the email's own tracking once, the way a person reading it would: the SmartBid read receipt pixel (`securecc.smartbidnet.com/External/RequestReadReceipt.aspx?sCommunicationId=...`), the SendGrid open pixel (`em.smartinsight.co/wf/open?upn=...`) and the "Click Here to View the Project" link (`securecc.smartbidnet.com/Main/Login.aspx?...` WITHOUT `iR`), each one GET, no redirect following. A ping failure never fails the harvest. The file downloads themselves do NOT show the GC "Downloaded": verified live 2026-10-01, SmartBid's per-file `DownloadedOn` did not move for any file this path fetched (876398 stayed 0 of 7, 874974 stayed at the 38 of 45 a person had downloaded). |
| Addenda for known projects | Not pulled (same as PipelineSuite): an addendum / RFI / reminder notice about a project we already have matches and goes `merged` / `exists`; merged rows are not harvested. |
| Files | Every file in the project's plan room (folders walked, `SetPlanRoom` included, deduped by FileId), one at a time, into one Ingestion Sandbox run (`source_kind = rfp_email`) through `add_upload_file`, so PDFs are verified and Office files go through the 0125 conversion. Sizes in the JSON are KB. Same caps as every harvester (`rfp_harvest_max_files`, `rfp_harvest_max_total_bytes`, per file `rfp_ingest_max_file_bytes`). |
| Dedup, failure, who sees it | Exactly RFP_HARVEST.md section 1: one `rfp_harvests` row per `external_key` (so the Bids@, office@ and tmoore@ copies of one invitation share one harvest), reused for `RFP_HARVEST_REUSE_DAYS`; transient waits/retries, permanent fails visibly and the email moves on. |
| Terms | Same call as PipelineSuite: no note to ConstructConnect; proceed as with Procore. |

---

## 2. The email (captured facts)

Plain text as stored in `rfp_emails.body_text`. Outlook wraps every link in
`nam09.safelinks.protection.outlook.com/?url=<encoded>`; unwrap with
`procore_client.unwrap_link`. The links, unwrapped:

```
[ClickHereBids189.gif] https://securecc.smartbidnet.com/Main/Login.aspx?cId=bp_1608767482&sPassportKey=<40 HEX>&sBidId=874974&st=101&e=1
[Bidboard.gif]         https://securecc.smartbidnet.com/External/ViewOnDigitalBidBoard.aspx?cId=bp_...&sPassportKey=...&sBidId=874974&st=116&e=1
If this link does not work, please go to https://securecc.smartbidnet.com/LEHW?st=102 and enter the access key: <15 hex>
Yes, I'll Bid All Codes        ...Main/Login.aspx?cId=...&sPassportKey=...&sBidId=874974&iR=1&st=103&e=1
No, I Won't Bid this Job       ...Main/Login.aspx?cId=...&sPassportKey=...&sBidId=874974&iR=0&st=104&e=1
Click Here to View the Project ...Main/Login.aspx?cId=...&sPassportKey=...&sBidId=874974&st=105&e=1
Unsubscribe                    ...External/Unsubscribe.aspx?DId=<comm detail id>&PId=<person id>&CType=1&st=106&e=1
```

The body then says who invited us ("Hello Estimating Department, Your
Company, G3 Electrical Technologies (LAS VEGAS, NV), was invited to bid:
<project>"), the notice text, the due line ("Your proposals are due no later
than October 1, 2026 05:00 PM."), and the GC person's signature (name, GC
company, address, phone, email). Notice kinds seen: ITB / Invitation to Bid,
Information for Bidders, Addendum Notification / Notice, Addenda Available
for Download, RFI Notice, Bid Date / Bid Day Reminder, Update to Project,
Important Notice, plus the GC's marketing blasts (open house, golf/clay
shoot), which the classifier already answers "no".

Rules for `parse_reference(body_text) -> SmartBidRef | None` (pure):

- Collect every `https?://...` URL in the text, unwrap each.
- Keep URLs whose host is `securecc.smartbidnet.com` (case-insensitive) and
  whose path is `/Main/Login.aspx` (case-insensitive), whose query parses to
  `cId` matching `^bp_(\d{1,12})$`, `sPassportKey` matching
  `^[0-9A-Fa-f]{40}$`, `sBidId` matching `^\d{1,12}$`, and which carry NO
  `iR` parameter (any value, any case). A URL with `iR` is discarded, never
  fallen back to.
- Prefer the one with `st=105`; else the first kept. None when none is kept.

`SmartBidRef(bid_project_id, comm_detail_id, passport_key, click_url)`
(frozen dataclass; `repr`/`str` never include the key or the click URL):

| property | value |
|---|---|
| `external_key` | `smartbid:{bid_project_id}` |
| `session_provider` | `smartbid` |
| `external_url` | `https://gocc.smartbid.co/#/projectlist` (no key; a person with their own SmartBid login lands on their project list) |
| `fingerprint` | sha256 hex of the key, first 16 |

The email HTML (Graph at run time, section 4 step 3) carries:

- `<img src="https://securecc.smartbidnet.com/External/RequestReadReceipt.aspx?sCommunicationId=<comm detail id>&oimg=1x1pic.gif">`;
- `<img src="http://em.smartinsight.co/wf/open?upn=...">` (SendGrid);
- the anchors above, wrapped in safelinks.

`parse_tracking(html) -> Tracking(read_receipt_url, open_url, click_url)`:
`read_receipt_url` = first `img` whose unwrapped src host is
`securecc.smartbidnet.com` and path `/External/RequestReadReceipt.aspx` with
a digits-only `sCommunicationId`; `open_url` = first `img` whose unwrapped
src host is `em.smartinsight.co` and path `/wf/open`; `click_url` = the
first anchor whose visible text (tags stripped, whitespace collapsed)
contains `view the project` (case-insensitive) and whose unwrapped href
passes the `parse_reference` link rules (no `iR`). An anchor whose text
contains `bid` together with `yes` or `no`, or whose href has `iR`, is never
returned (asserted by test). Fallback when Graph or the parse fails: the
click is `ref.click_url` and no pixel is fired.

---

## 3. SmartBid client (`app/services/smartbid_client.py`)

Pure HTTP, no Supabase import; the session store is injected (the same
`SessionStore` protocol Procore and PipelineSuite use). Tests drive it with
`httpx.MockTransport` over the captured JSON (fixtures in
`tests/fixtures_smartbid/`, every passport key, access key, bearer token,
security token, SAS signature, `upn` token and `cId` replaced by dummies).

### 3.1 Platform facts (captured 2026-10-01)

- `Main/Login.aspx?...` (IIS, ASP.NET): 302 to `/Login.aspx?<opaque>`, then
  302 to `https://gocc.smartbid.co/#/sbnpassport/<bid>?&goto=bp&cId=bp_<id>&passportkey=<key>`.
  The SPA (`App.UrlApi = https://apicc.smartinsight.co`) then does what
  this client does directly. Only the first hop is ever requested (the
  click ping); the client never follows it.
- Login: `POST https://apicc.smartinsight.co/token`,
  `application/x-www-form-urlencoded`: `grant_type=passport_key`,
  `bidid=<bid>`, `commdetailid=<comm detail id>`, `key=<passport key>`,
  `typepassportkey=bidproject_passportkey`, `bfId=` (empty),
  `isIframe=false`. Headers `Origin: https://gocc.smartbid.co`,
  `Referer: https://gocc.smartbid.co/`. 200 JSON: `access_token` (bearer,
  ~880 chars), `expires_in` (28799), `account_id` (the person id),
  `email`, `name`, `companylocation_id`, `system_id`, `bidProjectSystemId`
  (the GC's SmartBid system), `.expires`. A bad key, or a valid key sent
  with another project's bid id, answers `500 text/html` (an ASP.NET
  "Runtime Error" page), indistinguishable from an outage (captured
  2026-10-01). An OAuth-shaped `400 {"error": "invalid_grant"}` is handled
  too in case the platform starts sending it.
- Every API call: `Authorization: Bearer <token>`, `Accept: application/json`,
  the same Origin/Referer, `cache=false` style `_=<ms>` not required.
- `GET /api/projects/getconfidentialagreement?bidProjectId=<bid>&personId=<account_id>&isCcbc=false`
  -> `BidProjectConfidentialAgreement {ConfidentialityAgreement: bool,
  ConfidentialityAgreementSCA: int, Title, SystemId, SystemName, ...}`,
  `ConfidentialAgreementPerson: [{typeCA, StatusCA, ...}]`,
  `ConfidentialAgreementPAD: [...]`, `AlowedDetail: ""` (sic).
- `GET /api/projects/getbidproject?bidProjectId=<bid>&personId=<account_id>&bidProjectType=Invited&isIframe=false`
  -> JSON with `BidProject` (Title, SystemName, GCSystemName, Manager,
  Phone, Fax, Address1, Address2, City, State, Zip, BidDueDate
  `2026-10-01T17:00:00` (wall clock), FullBidDueDate `10-01-2026  5:00 PM`,
  TimeZoneShort `(PT)` / `(CT)` / `(MT)` (the GC system's zone: DC Building
  Group's says `(CT)` for a Las Vegas job; R&O and Martin-Harris say `(PT)`),
  Owner, Architect, ProjectDescription (HTML), ProjectStatus ("Open to
  Bid"), isPastDueDateTime, AllowLateProposal, PreBidMeetingDate,
  PreBidMeetingTimeZone, IsPreBidMeetingMandatory, PassportKey (the key
  again: never stored)), `BidInvitation` ([{Code, CodeName,
  CodePackageName, Status "Invited"/"Accepted"/..., StatusId, InvitedOn}]),
  `Localization`, `PlanRoom` and `SetPlanRoom` (each a list of root nodes:
  `{Name, isFile, Folders: [child nodes]}`; a file node has `isFile: true`,
  `FileId`, `Name` (extension kept), `Size` (KB), `FileIcon` (extension),
  `UploadedOn`, `Version`, `DownloadedOn`, `SCARequired`, `PQRequired`,
  `Href`), `AllFiles`, `sPlanRoomString` (HTML, ignored), `AzureOptions`.
  Folder names may carry trailing spaces.
- `Href` = `https://apicc.smartbidnet.com/project/fileMgmt/download?Value=<b64>`
  where `<b64>` decodes to `<FileId>.<BidProjectId>.<SystemId>`.
- Download, three requests per file (the SPA's `App.Utilities.getFileDirectUrl`):
  1. `timestamp` = UTC ISO with milliseconds and `Z` (JS `toISOString()`,
     `2026-10-01T21:08:10.123Z`).
  2. `POST https://apicc.smartinsight.co/api/admin/getSecurityToken`, bearer,
     `Content-Type: application/json; charset=utf-8`, body = the JSON string
     `"<Value><timestamp>"` -> 200 JSON string (the token, ~44 chars).
  3. `GET <Href>&token=<token>&timestamp=<timestamp>` (no extra encoding, as
     the SPA does) -> 200 `text/plain`: one URL,
     `https://azsblivenstorage.blob.core.windows.net/sbproductionstorage/Files/System_<sys>/BidsProjectFiles/BidProject_<bid>/<FileId>/<name>?sv=..&sr=..&sig=..&st=..&se=..&sp=..`
     (a short-lived SAS URL). Without token: 401, empty body.
  4. `GET` that blob URL with no cookies, no auth -> 200 bytes with the
     real content type (`Windows-Azure-Blob/1.0`). Verified: a 15,749 byte
     .docx.

### 3.2 Session and login

`SmartBidSession(config, store, ref, on_lock)` wraps one `httpx.Client`
(HTTP/1.1, `follow_redirects=False`, timeout
`smartbid_request_timeout_seconds`, browser `User-Agent` / `Accept` /
`Accept-Language`). `store` is the `rfp_harvest_sessions` adapter for
provider `smartbid`. Class attribute `provider = "smartbid"` (the sandbox
source kind).

`login()`: refuse while locked (`SmartBidLoginLocked`) or within
`smartbid_login_min_interval_seconds` of the last attempt
(`SmartBidUnavailable`, waits). POST `/token` as above. 200 with an
`access_token` -> keep the token, `account_id`, `system_id` in memory only;
`store.save_cookies("passport", [])` (nothing secret stored; it stamps
`logged_in_at`) and clear failures. 400 with `error == "invalid_grant"` ->
`SmartBidForbidden("SmartBid rejected this email's project link (the
invitation may have been withdrawn or the link expired).")`, NOT counted
toward the lock (the key is per email, not shared). 429 / 5xx / transport
-> `SmartBidTransient`, also NOT counted toward the lock: a 500 is what a
dead link gets, so one stale email must not pause every SmartBid harvest;
the job's own attempt cap ends it visibly (`last_error` says "SmartBid
refused the project link or is down (HTTP 500)"). Anything else (a 200
without `access_token`, another 4xx) -> `record_login(ok=False)`,
`on_lock(until, error, None)` once when the returned state carries
`locked_until`, raise `SmartBidLoginFailed("token", "<short reason>")`.
The key, the bearer token and the security tokens never appear in a log
line, an exception message, a stored row or a fixture.

`gate()`: GET `getconfidentialagreement`. 401 -> `SmartBidSessionExpired`
(the caller logs in once more and retries once). Non-empty `AlowedDetail`
-> `SmartBidForbidden("SmartBid does not allow this project: <detail>.")`.
General agreement required (`ConfidentialityAgreement` true and a person
row with `typeCA == 1`) or specific (`ConfidentialityAgreementSCA` 1, or 2
with `subIsInCodeWithSCA`) whose `StatusCA` is not 1, 3 or 6, or any
`ConfidentialAgreementPAD` row -> `SmartBidForbidden("This SmartBid
project needs a confidentiality agreement accepted in SmartBid first; open
it there, then press Harvest again.")`.

`get_project() -> dict`: GET `getbidproject`. 200 JSON with a `BidProject`
whose `BidProjectId` equals `ref.bid_project_id` -> return it. 401 ->
`SmartBidSessionExpired`. 404 -> `SmartBidForbidden`. 429 / 5xx /
transport -> `SmartBidTransient`. Any other 200 -> `SmartBidUnavailable`
("SmartBid answered with data the harvest does not understand.").

`download(entry, dest, max_bytes) -> int`: `entry` is a parsed file entry
(section 3.5). Build the `Value` from the entry (`FileId`, the project id,
the system id) and require it to equal the `Value` in the entry's `Href`
(else `SmartBidForbidden`). getSecurityToken -> direct URL -> blob, each
through `assert_allowed`. The direct-URL answer must be one `https` URL
whose host ends in `.blob.core.windows.net` and whose path contains
`/BidProject_<bid>/<FileId>/`; anything else -> `SmartBidForbidden`. The
blob GET uses a separate cookie-less client, no redirects, 1 MB chunks under
the running cap into `dest` (`O_EXCL`), unlinked on every failure. 404 or a
`text/html` answer -> `SmartBidForbidden`; over the cap ->
`SmartBidForbidden("larger ...")` (the service's `"larger"` test maps it to
`too_large`); 429 / 5xx / transport -> `SmartBidTransient`; 401 on the token
or direct-URL call -> `SmartBidSessionExpired` (the service re-logs in once
per file at most).

`ping(url) -> int | None`: GET with no redirects, 10 s timeout, only for
the three tracking shapes in section 3.4 (anything else raises
`ValueError`); returns the status, or None on any transport error. Paced.

Exceptions: `SmartBidError` base, `SmartBidTransient`,
`SmartBidUnavailable(locked_until=None)`, `SmartBidLoginLocked` (subclass of
Unavailable), `SmartBidLoginFailed(step, message)`,
`SmartBidSessionExpired`, `SmartBidForbidden`. Same `llm_error_kind`
markers as Procore's and PipelineSuite's.

### 3.3 Pace

Module-level `_pace()` like `pipelinesuite_client._pace` (its own lock and
clock, `smartbid_min_request_interval_seconds` x U(1, 2)), applied to every
request: the token, the two project reads, each security token, each
direct-URL lookup, each blob GET and each ping.

### 3.4 Allowlist and deny list (code, test-asserted)

Allowed, and the only requests the client can send:

| method | URL |
|---|---|
| GET | `https://securecc.smartbidnet.com/Main/Login.aspx?` with exactly the params `cId`, `sPassportKey`, `sBidId`, `st`, `e` (no `iR`, nothing else); tracking click only, never followed |
| GET | `https://securecc.smartbidnet.com/External/RequestReadReceipt.aspx?sCommunicationId=<digits>[&oimg=...]` |
| GET | `http(s)://em.smartinsight.co/wf/open?upn=...` |
| POST | `https://apicc.smartinsight.co/token` |
| GET | `https://apicc.smartinsight.co/api/projects/getconfidentialagreement?...` |
| GET | `https://apicc.smartinsight.co/api/projects/getbidproject?...` |
| POST | `https://apicc.smartinsight.co/api/admin/getSecurityToken` |
| GET | `https://apicc.smartbidnet.com/project/fileMgmt/download?Value=...&token=...&timestamp=...` |
| GET | `https://<account>.blob.core.windows.net/<container>/Files/System_<digits>/BidsProjectFiles/BidProject_<digits>/<digits>/<name>?<sas>` |

`assert_allowed(method, url)` runs before every request; a violation is a
`ValueError`. A test feeds every URL in the captured email text and HTML,
plus a list of known SmartBid routes, through it and asserts these are all
refused: any `Main/Login.aspx` with `iR` (both the Yes and the No links),
`/api/projects/setallcodesanswer`, `/api/projects/linkwontbidthisjob`,
`/api/projects/setisuploadedproposalfile`, any other `/api/...` path,
`External/Unsubscribe.aspx`, `External/ViewOnDigitalBidBoard.aspx`,
`/LEHW`, the second-hop `securecc.smartbidnet.com/Login.aspx?...`,
`gocc.smartbid.co` (the SPA), `constructconnect.com`, the safelinks host,
`sbn.cc`, and GET on `/token` or POST on `getbidproject`.

### 3.5 Parsing (pure)

`parse_project(data) -> SmartBidProject`:

```
SmartBidProject(
  bid_project_id: int, system_id: int | None, title, gc_name (SystemName, else GCSystemName),
  manager, phone, fax, address1, address2, city, state, zip,
  owner, architect, project_status, description_html,
  bid_due_local: "YYYY-MM-DDTHH:MM:SS" | None, bid_due_text (FullBidDueDate, whitespace squeezed),
  time_zone_short: "PT" | "CT" | ... | None   (parentheses stripped),
  past_due: bool, allow_late_proposal: bool,
  pre_bid: {"date", "time_zone", "mandatory"} | None   (None when the date is empty),
  invitations: [{"code", "name", "package", "status", "invited_on"}],
  files: [{"file_id", "name", "folder", "size_kb", "ext", "uploaded_on", "version", "href", "value", "restricted"}],
)
```

`files` walks `PlanRoom` then `SetPlanRoom` depth-first in order, dedupes
by `file_id` (first wins), skips nodes without a `FileId` or `Href`;
`folder` is the folder names (each stripped) joined with `/`, the root node
`root` omitted; `restricted` is `SCARequired or PQRequired` on the file or
any ancestor folder. `response_recorded` = any invitation whose status is
not `Invited` (informational only, never acted on).

`bid_due_at(project) -> str | None`: `bid_due_local` in the zone named by
`time_zone_short` (`PT` America/Los_Angeles, `MT` America/Denver, `AZ` or
`MST` America/Phoenix, `CT` America/Chicago, `ET` America/New_York, `AKT`
America/Anchorage, `HT` Pacific/Honolulu; unknown or missing ->
America/Los_Angeles and `bid_due_tz_assumed = true`) as an ISO instant, or
None. The zone is taken as SmartBid states it, never second-guessed; the
text is kept beside it so a reader can see what the portal said.

`classify_name` is `pipelinesuite_client.classify_name` (imported, not
copied).

---

## 4. Service wiring (`rfp_harvest.py`)

Vocabulary (`rfp_email_auth.py`): `METHOD_SMARTBID = "smartbid"`, in both
tuples after `pipelinesuite`. Router literals and `MethodPatchIn` /
`AuthorizedSenderIn` gain it (locked only, like `procore`).

- `harvester_for(row)`: `method == smartbid and s.smartbid_enabled` ->
  `"smartbid"`.
- `platform_reference(method, body_text)`: `smartbid` ->
  `smartbid_client.parse_reference`. `PlatformRef` gains `SmartBidRef`.
- `can_harvest(row)`: off -> `_MSG_NO_HARVESTER`; no reference -> "The
  email carries no SmartBid project link." ; else ok.
- `session_provider_for(row)`: `smartbid` (when a reference parses).
  `_SessionStore` and `availability` learn the `smartbid` provider
  (`smartbid_login_max_failures`, `smartbid_login_lock_seconds`);
  `availability_for` handles the method.
- `session_status()` gains `"smartbid": {"enabled", "logged_in_at",
  "last_used_at", "last_login_attempt_at", "login_failures",
  "locked_until", "last_error"}` from the `smartbid` row.
- `_notify_lock(...)`: a SmartBid sentence: "SmartBid logins failed
  repeatedly; SmartBid RFP harvests are paused until <time>. Each login
  uses the project link in the invitation email." Bell dedupe unchanged.
- `_harvest_files` / `_download_one` already take any session exposing
  `provider` and `download(...)`; add the SmartBid `Transient` / `Forbidden`
  pair to what they catch and pass the parsed entry the client needs (a
  small adapter is fine; the URL list stays in memory only).

`execute()` gains a `smartbid` branch beside the `pipelinesuite` one,
sharing `_find_or_create_harvest`, `_reusable`, `_claim_harvest`,
`_update_claimed`, `_harvest_files`, `_permanent`, `_done`, `_release`,
`_run_claimed`:

1. `ref = platform_reference(...)`; None -> permanent (the sentence above).
2. Find or create the harvest row (`external_url = ref.external_url`);
   reuse inside the window unless `force`; CAS-claim as PipelineSuite.
3. Tracking pings (skipped when `smartbid_tracking_pings_enabled` is off or
   the harvest row's `data.tracking.pinged_at` is set): Graph HTML for the
   email (the sighting for `primary_mailbox`, else any), `parse_tracking`,
   ping read receipt, then open, then click; fallback to `ref.click_url`
   only. Record `data.tracking = {"pinged_at", "opened": bool | None
   (either pixel 200), "clicked": bool | None (200 or 302), "error"}`.
4. `with open_smartbid_session(settings, ref) as session`: availability
   (lock -> park, no attempt spent); `login()`; `gate()`; `get_project()`
   (one re-login + retry on `SessionExpired`); `parse_project`.
5. Facts first (`facts_at`), then caps, then files (restricted entries get
   `status = "skipped"`, `error = "SmartBid requires an agreement for this
   file"`), then complete: the PipelineSuite order.

`data` (smartbid):

```
{
  "platform": "smartbid",
  "bid_project_id", "system_id",
  "project_name", "project_address" (address1, address2, city, state zip joined ", "),
  "bid_due_at", "bid_due_text", "bid_due_tz" ("PT"...), "bid_due_tz_assumed": bool,
  "gc": {"name", "address": null, "phone", "fax", "website": null},
  "point_of_contact": {"name": manager, "email": null, "phone"},
  "owner", "architect", "project_status", "past_due", "allow_late_proposal",
  "pre_bid": {...} | null,
  "invitations": [{"code", "name", "status"}],
  "response_recorded": bool,
  "tracking": {...},
  "documents": {"count", "bytes", "kinds": {...}, "folders": [...], "restricted": int}
}
```

`description_text` = `html_to_text(description_html)`; `instructions_text`
= None. `raw` = `{bid_project: <BidProject minus PassportKey and
ProjectDescription>, invitations, files head}`: no key, no token, no SAS
URL. `files[]` entries: `{file_path: "<folder>/<name>" (or "<name>"), size:
KB x 1024, kind, discipline: null, file_id, uploaded_on, sandbox_file_id,
status, error}`.

A test asserts the passport key, the bearer token, the security token and
the SAS signature appear nowhere in the harvest row, the email row's
updates, the queue row or any log record produced by `execute`.

---

## 5. Migration 0148 (`0148_rfp_smartbid_method.sql`)

Apply after 0147. Idempotent, the 0129 pattern:

- `rfp_emails_invitation_method_check`: `organic, procore, pipelinesuite,
  smartbid, gc_portal, general, nonorganic`.
- `rfp_authorized_senders_method_check`: `procore, pipelinesuite, smartbid,
  gc_portal, general`.
- Seed `('domain', 'smartbidnet.com', 'smartbid', true)` `on conflict (kind,
  value) do update set method = 'smartbid', locked = true`.
- `notify pgrst, 'reload schema'`.

Release step after applying: rows from `smartbidnet.com` parked at
`flagged_unauthorized` are re-authorized with
`rfp_email_ingest.rescan_after_rule_added` once for the seeded rule. On dev
that is about 55 rows across about 12 real projects: every new project the
match step does not find gets logged into and downloaded (hundreds of MB
each, and the GC sees the open and click pings), so it is run only with
the user's go.

---

## 6. Configuration

| env | default | meaning |
|---|---|---|
| SMARTBID_ENABLED | true | the harvester; `RFP_INGESTION_ENABLED` and `RFP_HARVEST_ENABLED` still gate it |
| SMARTBID_TRACKING_PINGS_ENABLED | true | fire the read receipt, the open pixel and the View the Project click once per harvest |
| SMARTBID_MIN_REQUEST_INTERVAL_SECONDS | 2.0 | pace floor, x U(1, 2); at least 0.5 |
| SMARTBID_LOGIN_MIN_INTERVAL_SECONDS | 30 | between token requests; at least 10 |
| SMARTBID_LOGIN_MAX_FAILURES | 3 | unexpected login answers before the lock |
| SMARTBID_LOGIN_LOCK_SECONDS | 21600 | 6 h |
| SMARTBID_REQUEST_TIMEOUT_SECONDS | 60 | |

`Settings.rfp_harvest_active` also counts `smartbid_enabled`. `.env.example`
documents the block.

---

## 7. API and frontend

- `GET /rfp-emails/{id}` unchanged in shape; `harvest.data.platform =
  "smartbid"` picks the grid. `POST /{id}/harvest` answers 503
  `rfp_harvest_locked` from the `smartbid` lock.
- `GET /rfp-emails/harvest-status` gains the `smartbid` block.
- Authorized senders: `method` accepts `smartbid` (locked only).
- FE `lib/rfpEmails.ts`: `InvitationMethod` and the rule method union gain
  `"smartbid"`; `RfpSmartBidHarvestData` typed as section 4;
  `RfpHarvestStatus.smartbid`.
- Settings rule picker: option "SmartBid".
- `RfpHarvestBlock.tsx`: `isSmartBidData` -> `SmartBidFactsGrid`: Project
  name, Address, Bid due (with the zone SmartBid states, and "zone assumed"
  when it did not), GC (name, phone), Contact, Owner, Architect, Status,
  Pre-bid meeting, Codes invited (code, name, status), Email signals
  ("Opened and clicked", "Clicked", "Not sent", with the time),
  restricted-file count when non-zero. Description block shows the project
  description.
- `RfpHarvestStatusSection.tsx`: a "SmartBid" block (last login, failures,
  lock, last error). No key, no token.
- Every method label: `rfpEmails.method.smartbid` = "SmartBid" in all six
  catalogs (the namespace is English in every catalog). Help center
  (`lib/help/rfp/senders.ts`, `itAdmin.ts`): SmartBid is now a built-in
  method with a seeded locked rule, not "no rule today". No em dashes.

---

## 8. Tests

- `tests/fixtures_smartbid/`: the three captured `getbidproject` answers
  (874974 big, nested folders, docx/xlsx, trailing-space folder names; 876398
  small; 874512 two-level addenda folders), trimmed of `sPlanRoomString` and
  `Integration`, keys replaced; the `getconfidentialagreement` answer plus
  synthetic "agreement required", "PAD" and "not allowed" variants; the
  token answer with a dummy token; the email texts (874974, 876398) and
  the 874974 HTML with dummies; a dummy direct-URL answer.
- `tests/test_smartbid_client.py` (MockTransport): reference parsing (st=105
  preferred, `iR` links never chosen even when they come first, upper/lower
  case hex, missing pieces -> None, unwrapped and bare links, `repr`
  without the key); tracking parsing (both pixels, the click, Yes/No never
  returned); the login (success, invalid_grant -> Forbidden without a lock
  count, unexpected -> LoginFailed with the lock after N and one bell, min
  interval); the gate (open, each agreement shape, AlowedDetail); project
  parsing over all three fixtures (folders, dedupe across PlanRoom and
  SetPlanRoom, restricted inheritance, KB, `bid_due_at` for PT, CT and an
  unknown zone); the download chain (Value cross-check, the direct-URL host
  and path rules, cap, html-as-file, 401 -> SessionExpired); the allow/deny
  list over every URL in the email and the route list; pace.
- `tests/test_rfp_harvest.py` additions: `harvester_for` /
  `platform_reference` / `can_harvest` / `session_provider_for` /
  `availability_for` for the method; `execute` against a stub session and a
  stub Graph: pings once and recorded, skipped on a later run, never fatal;
  the gate's Forbidden is permanent; facts before files; KB -> bytes; caps;
  restricted files skipped; failure mappings; the lock parks without an
  attempt; no secret persisted or logged; `session_status` block.
- `tests/test_rfp_email_auth.py`, `test_rfp_emails_router.py`: vocabulary,
  the locked rule with method `smartbid`, `MethodPatchIn`, the migration
  file test in the 0129 style.
- FE: `npx tsc --noEmit`, `npx eslint .`, `next build` clean.

---

## 9. Out of scope

- The SmartBid project-list sweep and any addenda refresh for known
  projects.
- The access-key login (`/LEHW` + the 15-hex access key): the passport link
  covers every email seen.
- Production migration, Railway variables and the backlog rescan (explicit
  approval).

---

## 10. Live verification on dev (2026-10-01)

0148 applied to dev (`bpidntbyvoooqvaispup`) only. Two parked rows were
released by hand (the rescan's per-row CAS, nothing else touched) and the
running worker took them through unaided:

| | UNLV Dental (Martin-Harris 876398, rfp_emails `3558a058`) | NSU Gateway (DC Building Group 874974, rfp_emails `d6f1c05c`) |
|---|---|---|
| authorize / method | `smartbid` via the seeded locked rule | same |
| extract | GC "Martin~Harris Construction", project name right | GC "DC Building Group", project name right |
| tracking | opened = true, clicked = true | opened = true, clicked = true |
| facts | due 2026-10-29 11:00 PT | due 2026-10-01 5:00 PM CT (as SmartBid states it) |
| files | 7 of 7 fetched from SmartBid; 6 into the sandbox | 45 of 45 fetched; 41 into the sandbox |
| SmartBid response afterwards | still "Invited" (no answer recorded) | still "Accepted" (a person's 9/21 answer, unchanged) |
| "Downloaded" marks | 0 of 7 (unchanged) | 38 of 45 (unchanged) |

Secrets: no passport key, bearer token, security token, SAS URL or
signature in `rfp_harvests`, `rfp_harvest_sessions` (account `passport`,
cookies `[]`) or the queue rows.

The 5 files that did not reach the sandbox (21, 115, 140, 154 and 210 MB)
failed in the sandbox's own quarantine upload, not in SmartBid: reproduced
without SmartBid, a 150 MB `rfp_ingest_storage.upload_bytes` from the dev
machine fails all three attempts with `SSL: SSLV3_ALERT_BAD_RECORD_MAC`
(the TLS stream to Supabase Storage is corrupted mid-upload; the request
never reaches the storage logs). Same failure class as the 267 MB file
another run hit the same day and a 21 MB one on 2026-09-21. Open item for
the sandbox: resumable (chunked) uploads for large files.

Fix found by the test: every SmartBid file classified `other` because the
names use underscores (`\b` does not break on `_`). `classify_name` now
treats underscores as spaces and never calls a spreadsheet or Word file a
drawing, and SmartBid falls back to the folder name ("Shell Bid Set",
"Plans/Bid Drawings") when the file name is only a sheet code.

### 10.1 Backlog run (2026-10-02)

The user's go: "let the app clear the backlog". The app's own path ran
(`rescan_after_rule_added` for the seeded `smartbidnet.com` rule, the same
call the authorized-senders route makes): 28 parked rows from the 14-day
learn-back re-ran in 27 s. The 29 older rows (2026-09-08 to 09-17) stay at
`flagged_unauthorized` by design (outside the learn-back).

- Every row authorized `smartbid` and extracted the right GC (R&O
  Construction, Martin-Harris Construction, DC Building Group).
- One harvest per project (`external_key` unique): 7 projects, every notice
  about a project (ITB, addenda, RFI notices, reminders, Information for
  Bidders) reused it; the office@/tmoore@ copies of one ITB closed as
  siblings. The five new projects took every file: Havana Bar 17/17 (194 MB),
  Catalyst 20/20 (214 MB), NSU Site 3 19/19 (739 MB), Murphy 8021 13/13 (246
  MB), Whole Foods 6/6 (110 MB); kinds now split drawing / specification.
- Red line, checked against a read-only snapshot taken before the rescan:
  all 14 (project, recipient) pairs kept the same SmartBid response
  (Invited stayed Invited, a person's Accepted stayed Accepted) and the same
  `DownloadedOn` count.
- No passport key, token, SAS URL or signature in the 7 harvests, the 125
  sandbox files or the queue rows; the `smartbid` session row has no
  failures, no lock, `cookies = []`; no lock bell.
- Interruptions recovered on their own: a dev `--reload` (another session's
  edits) and, at 08:24 PDT, a SIGSEGV of the API worker inside PDFium
  (`FPDF_LoadPage`, called by `pdf_split.render_pages` for the Bid File
  Splitter while three split jobs ran together; not SmartBid code). The
  interrupted harvests and split jobs re-ran and finished (accepted files
  kept); the RFP intake lease the dead worker held blocked the sweep until
  it expired (33 minutes). Open item outside this slice: PDFium in the API
  process (finalizers outside the lock; move rendering to a child process).

Final state (09:51 PDT): every row terminal (30 `done`, 4 of them siblings;
3 `flagged_llm_no` from before), every sandbox run and split complete. The
five projects harvested today verified 93 of 95 files; the two misses are
the Phase I ESA reports of Murphy 8021 and Whole Foods, which the sandbox
was rendering when the worker crashed: the requeued run did not reprocess
them (left `failed` / `interrupted`, "retry the run"). Sandbox open item:
a file interrupted by a crash or deploy is not retried automatically.
