# RFP Ingestion: BuildingConnected Bid Board

Design record for the BuildingConnected slice of RFP Ingestion. The company
receives most GC invitations through BuildingConnected (Bid Board Pro). The
application reads the Bid Board through the Autodesk Platform Services (APS)
BuildingConnected API v2, decides with the existing matcher whether each
opportunity is already a project, resolves the GC against the application's
GC list with a confirmed alias table, and creates the project with the facts
BuildingConnected has. Files never come from BuildingConnected (the API has
none); the project carries a "pull the files" flag until a person downloads
them from the web app and uploads them.

It is the second portal behind the NGEM tables (`RFP_NGEM_PORTAL.md`): same
`rfp_portal_invitations` / `rfp_portal_runs`, same tab family on
`/rfp-emails`, same matcher (`rfp_match.py`), same creation service
(`rfp_create.py`). It does NOT use the sandbox, the harvest job, the file
splitter or the LLM classify step: there is nothing to classify (every row is
an invitation the GC addressed to us) and nothing to download.

Status: design v1, 2026-09-26, written from the API reference (190 pages
mirrored to `BDR/scratch/buildingconnected_api_docs/`) and a live pull of the
company's whole Bid Board (2,543 opportunities, `live_pull_2026-09-26.json`
next to the mirror) with the Traditional Web App the user registered on
2026-09-26. Nothing here writes to BuildingConnected; the token is
`data:read` only.

Naming, used everywhere: setting prefix `rfp_bc_` (env `RFP_BC_`) plus
`building_connected_` (env `BUILDING_CONNECTED_`, the existing client id and
secret), pure client `app/services/bc_client.py`, portal class
`BuildingConnectedPortal` registered in `rfp_portal_ingest.PORTALS` under
`portal = 'buildingconnected'`, GC alias service `app/services/gc_aliases.py`,
migration `0136_rfp_buildingconnected.sql`, queue job types `rfp_portal_scan`
(reused) and `rfp_bc_full_sync`, router additions on `/rfp-portal` plus
`/rfp-portal/buildingconnected/*` for OAuth, bell types
`rfp_bc.new_invitations`, `rfp_bc.invitation_changed`,
`rfp_bc.scan_failed`, `rfp_bc.disconnected`, FE tab "BuildingConnected" on
`/rfp-emails`, FE namespace `rfpPortal` (shared), harvest method
`buildingconnected`, `projects.invitation_method = 'buildingconnected'`.

---

## 1. Decisions locked in (2026-09-25/26, with the user)

| Topic | Decision |
|---|---|
| Direction | Read-only extraction into BDR. Never act inside Bid Board (no status changes, no bids, no comments). Scope `data:read`. |
| Auth | 3-legged OAuth (the API refuses 2-legged: "You are not authorized to use a 2-legged token", verified). One user with `bidBoardPermissions.viewAll` connects once from Settings; the refresh token keeps the connection alive unattended. |
| Steps | Scan, GC resolve, match, create. No sandbox, no harvest, no split, no classify. |
| Matching | Required. Same project from several GCs and re-invites from the same GC are the matcher's job (BuildingConnected does not link them). |
| GC | Resolve the BuildingConnected company to a BDR GC through a confirmed alias. Unconfirmed: propose the most similar GC only when it scores at least `gc_aliases.PROVISIONAL_MIN_SCORE` (0.5) and ask the user on the project ("BuildingConnected says X, BDR has Y, same GC?"); below that there is no GC (kind `none`, the closest names still listed as candidates) and the row parks for a person. Yes saves the alias; No lets the user pick another GC or add one inline, and saves that. Never ask twice for the same company. A GC swapped on the board (a new company id) is re-resolved on a row with no project and asked again on a project (section 3.6). |
| GC guard | While the GC is unconfirmed the matcher treats it as unresolved: ambiguous cases park at `review_match` instead of auto-merging under a guessed GC. |
| Files | None from BuildingConnected. A `files_needed` flag on the project (new project, or a merge target with no drawings/specs), with the deep link. Auto-clears when the first drawing or specification lands; manual Dismiss too. |
| Fields pulled | name, GC + lead contact, due date, job walk (new column), expected start/finish (existing `est_start_date` / `est_finish_date`), invited date, location, project information + trade-specific instructions (two new columns, shown as one "Project details" block with two labels). |
| Due-date moves | Notify Estimating Admins and show "GC moved the due date to X" on the project with Apply; never overwrite `actual_bid_at` silently. |
| Lead contact | Create the GC contact when missing and attach it as the project's GC bid contact, only once the GC is confirmed (alias, contact or domain, or the user's answer on the GC card, "same GC" included); never under a provisional guess. |
| Backfill | Whole board into the mirror; only rows passing the entry rule (section 3.3) enter the pipeline. Historical rows visible in the tab, "Process" by hand. Creation stays button-first (`RFP_CREATE_AUTO_ENABLED`). |
| Buckets | Undecided, Accepted (`WILL_SUBMIT`) and Submitted all pulled. Undecided and Accepted create; Submitted matches, and an unmatched Submitted row parks in review ("submitted in BuildingConnected, no BDR project"). |
| Expired | Past-due rows at backfill are mirror-only. A pipeline row 7 days past due parks as `expired` (restorable). No due date never expires. |
| No due date | Enters the mirror and the tab, parks in review with reason `no_due_date`, never auto-creates (the live pull shows most of these are outreach notices, not bids). |
| NDA rows | Masked by the API (no GC, no info, no `updatedAt`). Park in review with reason `nda_required` and the deep link; re-read nightly; they proceed once BuildingConnected exposes the details. |

---

## 2. Facts from the live pull (2026-09-26, the real board)

- 2,543 opportunities, 296 distinct GC companies, 11 BC users, one office
  (Las Vegas). `offices[].hasBbPro` is `false` on the office; the licence is
  assigned to the user, and `GET /opportunities` works.
- Page size max is 100 (`limit=200` is a 400 "query.limit should be <= 100").
  26 requests, 19.5 s, 13 MB for the whole board (about 5 KB per row).
- `GET /opportunities/{id}` returns exactly the list object (no extra keys).
  `GET /bids/{id}` for our own bid is 403 `BC_PRO_SUBSCRIPTION_REQUIRED`.
  Comments exist but are empty.
- `source`: 2,538 BUILDINGCONNECTED, 3 MANUAL, 1 EMAIL, 1 DISCOVERED.
- `submissionState`: 2,150 UNDECIDED, 193 SUBMITTED, 136 WILL_SUBMIT,
  64 DECLINED. 2,196 archived, 347 active. Active by state: UNDECIDED 53
  (16 future, 17 no due, 20 past due at most 11 days), WILL_SUBMIT 104
  (5 future, 2 no due, 97 past due, median 122 days), SUBMITTED 190 (all past
  due). The entry rule admits 40 rows today, 19 of them with no due date.
- 58 rows have `isNdaRequired = true` and are masked: `client.*` null,
  `projectInformation` empty, `location` null, `workflowBucket` null,
  `invitedAt` null and `updatedAt` null. Because `updatedAt` is null they
  never appear in a `filter[updatedAt]` poll; only a full pull sees them.
- Top-level dates never differ from `clientValues` on this board (nobody
  edits them locally); `clientValues` is null on the 5 foreign rows.
- `updatedAt` moves for reasons beyond GC edits (every row has
  `updatedAt != createdAt`; 87 rows updated in the last 30 days were invited
  earlier). Polling on it over-returns a little, which is harmless.
- Same name from more than one GC: 40 names (11 in 2026). Same GC and same
  name: 127 groups; 86 invited the same day, 78 with different `tradeName`
  (Electrical + Low Voltage packages of one project), 14 spanning years
  (rebids). Both shapes must land on ONE project.
- No due date rows on the active board are mostly notices ("OUTREACH NOTICE",
  "QUARTERLY PROJECTS UPDATE", "Updating Company Information Request",
  "Project Information Meeting") with a few real invitations mixed in.
- Field usage: `number`, `customTags`, `additionalInfo`, `competitors`,
  `winProbability`, `rom` are unused; `priority` is always UNKNOWN;
  `outcome.state` is UNKNOWN on every row but one; `marketSector` NONE;
  `location.precisionLevel` always null; `location.complete` set on 2,470.
  `jobWalkAt` set on 365, `expectedStartAt` 1,280, `expectedFinishAt` 1,114,
  `rfisDueAt` 797, `architect` 1,084, `projectSize` 1,501, lead phone 1,777.
- `projectInformation` is HTML (`div`, `br`, `b`, `li`, `ul`, `u`, `i`, `a`),
  median 560 chars, max 32 KB. `tradeSpecificInstructions` set on 368 rows,
  median 165 chars, max 4.5 KB.
- `invitedAt` differs from `createdAt` by more than a day on 130 rows: use
  `invitedAt` for `invitation_at`, `createdAt` as the fallback.
- Refresh tokens rotate on every refresh and the previous one dies at once
  (`invalid_grant`); access tokens last 3,599 s. Verified.
- Rate limit 1,000 requests per minute per user; 429 carries `Retry-After`.

---

## 3. Pipeline

```
scan (15 min, filter[updatedAt]) ─┐
full sync (nightly 02:30 PT) ─────┴─> upsert rfp_portal_invitations (key = opportunity id)
                                        │
                                        ├─ mirror-only (historical / archived / declined / past due at backfill)
                                        │
                                        └─ status = match
                                              │  gc resolve (alias | domain | similarity)
                                              v
                                    rfp_match.rank_candidates + route
                                     ├─ exists ────> project marker (+ GC link when new to the project) (+ files_needed when no files)
                                     ├─ review_match ─> BuildingConnected tab, Review dialog (confirm GC there too)
                                     └─ create ─────> button ("Create project"), or automatic behind RFP_CREATE_AUTO_ENABLED
                                                        rfp_create.create_from_portal -> Go/No-Go, intake task, flags
```

### 3.1 Scheduler

`rfp_portal_ingest.poll_once` already claims slots per portal. The
BuildingConnected portal registers two kinds of slot:

- Incremental scan every `RFP_BC_POLL_MINUTES` (15) around the clock, claimed
  through `rfp_portal_runs (portal = 'buildingconnected', scheduled_for =
  slot)` exactly like NGEM. The scan job is the shared `rfp_portal_scan`
  with `portal` on the run row; `BuildingConnectedPortal.scan(run)` does the
  work.
- Full sync once a day at `RFP_BC_FULL_SYNC_TIME` (02:30 Pacific), a run row
  with `trigger = 'scheduled'` and `kind = 'full'` (new column on
  `rfp_portal_runs`, default `incremental`). It pages the whole board and is
  the only pass that sees the NDA-masked rows and deletions.

Both respect the partial unique index (one active run per portal); a full
sync that finds an incremental running waits for the next tick. "Run now" on
the settings block enqueues a manual incremental run; "Full sync now" a
manual full run.

### 3.2 The scan (`BuildingConnectedPortal.scan`)

1. Token: `bc_client.access_token(sb)` (section 4.2).
2. Incremental: `GET /opportunities?filter[updatedAt]=<high_water minus
   RFP_BC_OVERLAP_MINUTES (30)>..&limit=100`, follow `pagination.cursorState`
   until absent. Full: the same without the filter. The high-water mark is
   the run's `started_at`, stored on the portal's `rfp_portal_state` row
   (new table, section 5) only when the run completes; a failed run leaves it
   alone so the next run re-covers the window.
3. For every result, `_row_key = (portal, agency_key = 'bc', bid_number =
   opportunity id)`. New id: insert (3.3 decides the status). Known id:
   compare the tracked fields (3.6), update the mirror and the volatile
   columns, append the change log.
4. Full sync only: every pipeline row (status in `match`, `review_match`,
   `create`) whose id was not in the pull gets `missing_since = now()`; after
   `RFP_BC_MISSING_DAYS` (3) of absence it parks as `withdrawn` with the
   reason. Mirror-only rows that vanish just get `missing_since` (the GC
   withdrew the invite or Bid Board deleted a foreign row).
5. Counts on the run row, bell `rfp_bc.new_invitations` (deduped per run)
   when the run inserted pipeline rows, `rfp_bc.scan_failed` to IT Admins on
   failure (the NGEM rule). A 401 whose refresh also fails marks the
   connection `disconnected` and rings `rfp_bc.disconnected` (once, until
   reconnected); scans stop until someone reconnects.

### 3.3 Entry rule (status on insert)

A row enters the pipeline (`status = 'match'`) only when ALL hold:
`isArchived = false`, `submissionState in (UNDECIDED, WILL_SUBMIT,
SUBMITTED)`, and (`dueAt` is null or `dueAt >= now`). Otherwise
`status = 'historical'` (mirror-only; visible under the Historical lane;
"Process" moves it to `match` by hand).

Refinements applied at the match step, not at insert:

- `isNdaRequired` with a null `client.company.id` (the masked shape):
  `review_match`, `flag_reason = nda_required`. Every scan that later sees the
  row populated (company id present) moves it back to `match`.
- `dueAt` null: `review_match`, `flag_reason = no_due_date`, after the
  matcher ran (a confident match to an existing project still resolves to
  `exists`; only the create path is withheld).
- `submissionState = SUBMITTED` with no confident match: `review_match`,
  `flag_reason = submitted_unmatched`.

A row already in the pipeline whose state changes in BuildingConnected:
`DECLINED` parks it as `ignored` (system, reason "declined in
BuildingConnected"); `isArchived = true` on a row still at `match` or
`review_match` parks it as `ignored` (system, "archived in
BuildingConnected"); neither touches a project that already exists (the
marker stays and the change shows on the project's RFP block).

### 3.4 GC resolve (`gc_aliases.resolve(sb, company_id, company_name,
lead_email)`)

Order:

1. Confirmed alias `gc_external_aliases (source = 'buildingconnected',
   external_id = company_id)` -> `GcResolution(gc_id, contact_id, kind =
   'alias')`. Done, no prompt.
2. Lead email: an existing `gc_contacts.email` equal to the lead's address
   -> that contact's GC, kind `contact`; else a non-public lead domain owned
   by exactly one GC's contacts -> kind `domain` (the `rfp_match.resolve_gc`
   rules reused). These two are treated as confirmed for matching but still
   ask on the project once ("BuildingConnected: Rafael Construction Inc;
   BDR: Rafael Companies (matched by rafaelcompanies.com). Same GC?") and
   save the alias on Yes. On No the alias points wherever the user chooses.
3. Similarity: `gc_aliases.provisional_score(company_name, gc.name)` over
   `general_contractors` (stricter than `rfp_match._similarity` about
   generic words and a single shared word); the best GC becomes the
   provisional GC only when it scores at least
   `gc_aliases.PROVISIONAL_MIN_SCORE` (0.5), with the top three kept as
   candidates for the picker. Kind `provisional`; NOT confirmed. (This
   replaces the first decision, "the most similar GC whatever the score":
   weak single-word guesses were landing on the wrong GC.)
4. Otherwise (no GC in the application, no company name, or the best score
   below 0.5): kind `none`, no GC attached. The candidates are still listed,
   so the card offers the closest names; the row parks for a person
   (`match_gc_unresolved` through the match guard, or the auto-create gate),
   and the picker opens on Pick / "Add a GC" with the BuildingConnected name
   and lead pre-filled.

The lead contact is filed under a GC only once that GC is confirmed: a
provisional GC is linked to a created project (with `gc_confirm_pending`)
but gets no `gc_contacts` row from the creation; the card's answer attaches
the lead through `rfp_create.swap_project_gc`, "Yes, same GC" included (an
unchanged GC: the lead as bid contact, the flag cleared).

The row stores `gc_external_id`, `gc_external_name`, `gc_id` (resolved or
provisional), `gc_kind`, `gc_candidates jsonb`, `gc_confirmed_at`.

Confirmation (`POST /rfp-portal/{id}/gc` with `{gc_id}` or `{create:
{name, contact_name, contact_email, contact_phone}}`): writes the alias
(unique on `(source, external_id)`), sets `gc_confirmed_at`, and when the
project already exists swaps the project's GC link and bid contact from the
provisional GC to the confirmed one (the provisional link is removed only when
no proposal was sent to it; otherwise both stay and the card says so). The
same endpoint serves the card on the project page and the card in the
BuildingConnected tab's Review dialog. `PATCH` on an alias (IT Admin,
settings) re-points it and a delete removes it; both follow through to the
invitations resolved through that alias: a row with no project gets the new
GC (or, on a delete, loses it and a row parked for the unresolved GC goes
back to `match`), a row on a project gets a `gc_alias` change-log entry and
its project `gc_confirm_pending` again. The project card serves the linked
row whose own question is open (a `gc_external_id` or `gc_alias` entry newer
than its `gc_confirmed_at`, the newest first), else the creator row, else
the newest merge; that row's answer clears the flag unless the creator's
own question is still open.

Name drift: when BuildingConnected renames a company, the alias holds by id
and `external_name` is updated; the card never re-opens (the rename is a
`gc_external_name` change-log entry only).

GC swapped on the board (a new `gc_external_id` on a known row): on a row
with no project the resolution (`gc_id`, `gc_kind`, `gc_candidates`,
`gc_confirmed_at`, `gc_confirmed_by`) is cleared and a `review_match`,
`create` or `done` row goes back to `match`, where the sweep resolves the
new company (the match write is fenced on the company id the tick read, so
a stale tick never writes the old company's GC back). On a row with a
project nothing on the project is rewritten: `gc_confirm_pending` is set,
the row's GC becomes a provisional guess (confirmation cleared, candidates
emptied, `gc_id` and `project_gc_id` kept for the swap), and the change
bell rings (section 3.6).

### 3.5 Match (`_step_match`, shared, with BuildingConnected facts)

`ExtractedFacts(project_name = name, gc_name = external company name,
bid_due_at = clientValues.dueAt or dueAt, has_time = true when the instant is
not midnight Pacific, bid_notes = text of tradeSpecificInstructions or the
first 400 chars of projectInformation)`. `route(...)` receives:

- `gc_resolved = gc_kind in (alias, contact, domain)`; provisional and none
  are unresolved (the guard).
- `gc_on_project` per candidate from `project_gcs`.
- `sender_verified = true` (the platform authenticated the GC).
- `auto_merge` = the existing `RFP_MATCH_AUTO_MERGE_ENABLED`.

Outcomes:

- `exists`: `match_project_id` set, marker on the project ("Also invited
  through BuildingConnected by <GC> on <date>", with the deep link and the
  BuildingConnected package trade). When the resolved GC is not on the
  project: `_insert_link(project_gcs, needs_by = due date)` plus the lead as
  a project GC bid contact. When the project has no drawing or
  specification files: `files_needed` set (3.8). Two invites for one project
  from the same GC on the same day (Electrical + Low Voltage packages) land
  here on the second row; the marker lists both trades.
- `review_match`: the tab's review dialog, the NGEM candidates UI reused,
  plus the GC card when the GC is unconfirmed.
- `create`: `create_from_portal` when the button is pressed, or at once when
  `RFP_CREATE_AUTO_ENABLED` and the row has a due date and a resolved or
  provisional GC (never `none`, never `nda_required`).

Rebids: `rfp_match.rebid_lookup` flags a closed or lost project with the
same name and GC (14 groups span years on this board); the row shows
"Possible rebid of <number>" and the create path carries the existing rebid
handling.

### 3.6 Change tracking

Tracked fields on a known row: `clientValues.dueAt`/`dueAt`, `jobWalkAt`,
`expectedStartAt`, `expectedFinishAt`, `name`, `submissionState`,
`isArchived`, `location.complete`, and the client company (`client.company.id`
as `gc_external_id`, `client.company.name` as `gc_external_name`). Each change
appends `{at, field, old, new, run_id}` to `change_log` (cap 50). An alias
repoint or delete adds `{field: "gc_alias", old, new}` (GC ids, `run_id`
null) on linked rows.

A `review_match` row parked `no_due_date` goes back to `match` when the due
date appears on the board; one parked `nda_required` does when the company
appears.

When the row is linked to a project (`match_project_id` or
`created_project_id`):

- Due date, job walk, start or finish moved: `rfp_bc.invitation_changed`
  bell to Estimating Admins and the project's RFP block shows "GC moved the
  due date from A to B" with an Apply button (`POST
  /rfp-portal/{id}/apply-dates`, writes `actual_bid_at` and the others,
  audit via the existing project change events). Nothing is applied
  silently.
- GC swapped (a new company id): the same bell, "GC changed: <old company>
  to <new company>", and the GC question reopens on the project (3.4). A
  rename of the same company is logged only.
- Name changed: shown on the block, never applied.
- Location changed: shown, Apply available.

The bell is one per Estimating Admin per invitation: while a user's bell
for the invitation is unread, a later change rewrites that bell in place
(message, `fields`, `run_id`, and `created_at` moved to now) instead of
adding a second one or staying silent; a user whose bell was read or
dismissed gets a new one.

### 3.7 Create (`rfp_create.create_from_portal`, BuildingConnected branch)

`facts_for_bc(inv)`:

| project column | source |
|---|---|
| name | `name` (clientValues.name when present) |
| actual_bid_at | `clientValues.dueAt` or `dueAt`; null allowed (flag `no_due_date`) |
| bid_time_unknown | false when the instant is not midnight Pacific |
| invitation_at | `invitedAt`, else `createdAt` |
| job_walk_at (NEW) | `jobWalkAt` |
| est_start_date / est_finish_date | `expectedStartAt` / `expectedFinishAt` as Pacific dates |
| address | `location.complete` |
| bidding_url | `https://app.buildingconnected.com/opportunities/{id}/info` |
| project_information (NEW) | `projectInformation` HTML -> text (block tags to newlines, list items to "- ", links kept as "text (url)"), cap `RFP_BC_TEXT_MAX_CHARS` (20,000) |
| trade_instructions (NEW) | `tradeSpecificInstructions`, same conversion |
| bid_notes | first 400 chars of trade_instructions, else of project_information (what the matcher and the intake task show) |
| notes | "Created from BuildingConnected: <GC>, package <tradeName>, invited <date>. <deep link>" |
| invitation_method | `buildingconnected` |
| is_budgetary (the B suffix) | `requestType = BUDGET` |

GC: `GcPlan(kind = GC_RESOLVED)` for alias/contact/domain, a new
`GC_PROVISIONAL` kind (links the GC like resolved but files no lead contact,
sets `gc_confirm_pending = true` on the project) for provisional, `GC_CREATED`
when the user chose "Add a GC" in the card before creating, `GC_NONE` when
no GC scored at least 0.5 (the auto-create gate parks such a row for a
person; the Create button makes the project without a GC).

Lead contact (confirmed GC only; a provisional GC gets it from the card's
answer): `gc_contacts` lookup by email under the resolved GC; create
when missing (`first/last name`, email, phone from `client.lead`); attach as
the project's GC bid contact (0110).

Everything else is the existing `_create`: number `YY.M.NNNN[B]`, Go/No-Go
via `review`, Estimating Admin intake task, Executive and Estimating Admin
notifications, the "Created from RFP Ingestion" page row with flags.

### 3.8 The files flag

`projects.files_needed_source` (`buildingconnected`), `files_needed_url`
(the deep link), `files_needed_set_at`, `files_needed_cleared_at`,
`files_needed_cleared_by`. Set on creation and on an `exists` merge whose
project has no file in the `drawing`, `electrical_drawing` or
`specification` categories. Cleared automatically by the file-add path when
the first file in one of those categories lands (a trigger-free service
check in the upload handler), or by `POST /projects/{id}/files-needed/dismiss`.

Surfaces: a banner on the project page ("Files were not pulled from
BuildingConnected. Download them there and upload them here." with the link
and Dismiss), the flag column on "Created from RFP Ingestion", a line on
the Estimating Admin intake task, and a count on the BuildingConnected tab's
header.

### 3.9 Expiry and withdrawal

Nightly (inside the full sync): pipeline rows with a due date more than
`RFP_BC_EXPIRE_DAYS` (7) in the past park as `expired` (system, reason "due
date passed on <date>"); rows absent from the board for `RFP_BC_MISSING_DAYS`
park as `withdrawn`. Both are restorable ("Restore" puts them back at
`match`). Rows linked to a project are never expired or withdrawn; the
project's lifecycle governs.

---

## 4. Client and OAuth (`app/services/bc_client.py`)

### 4.1 App registration (done 2026-09-26)

APS app of type Traditional Web App with the BuildingConnected API enabled;
callback URLs registered per environment: `http://localhost:8080/callback`
(local spike), plus the backend's `https://<railway host>/rfp-portal/
buildingconnected/callback` for staging and prod. Client id and secret are
the existing `BUILDING_CONNECTED_CLIENT_ID` / `_SECRET`.

### 4.2 Tokens

Table `rfp_oauth_connections` (section 5), one row per `provider`
(`buildingconnected`). Columns hold the plain-text (not app-encrypted)
`access_token`, `refresh_token`, `expires_at`, `connected_by`,
`connected_at`, `bc_user_id`, `bc_user_name`, `bc_company_id`,
`view_all`, `status (connected | disconnected)`, `last_refresh_at`,
`last_error`, `refresh_lock_until`, `refresh_lock_owner`. RLS forced,
service role only (the `rfp_harvest_sessions` pattern). Tokens are never
returned by any endpoint and never logged.

`access_token(sb)`:

1. Read the row. If `expires_at - 120 s > now`: return the access token.
2. Otherwise claim the refresh: `update ... set refresh_lock_until = now() +
   30 s, refresh_lock_owner = <worker id> where provider = ... and
   (refresh_lock_until is null or refresh_lock_until < now())` returning the
   row. No row returned: another worker is refreshing; sleep 1 s, re-read,
   retry up to 30 s. This matters because the refresh token rotates and the
   old one dies at once: two workers refreshing concurrently would kill the
   connection.
3. `POST /authentication/v2/token grant_type=refresh_token` with basic auth.
   On 200: write `access_token`, `refresh_token`, `expires_at`,
   `last_refresh_at`, clear the lock, in one update. On `invalid_grant`:
   `status = disconnected`, `last_error`, bell `rfp_bc.disconnected` to IT
   Admins, raise `BcDisconnected`. Other errors: release the lock, raise.
4. The write of the new refresh token happens before any API call uses the
   access token (a crash between the refresh and the write loses the
   connection; the write is the very next statement).

The 15-minute poll refreshes long before the 14/15-day refresh-token life;
the connection dies only when the backend is down for two weeks or the
Autodesk user loses the licence.

### 4.3 Connect flow

- `GET /rfp-portal/buildingconnected/connect` (IT Admin or Executive):
  builds the authorize URL with a signed `state` (HMAC of a nonce + the
  actor id, 10-minute expiry, stored in `rfp_oauth_states`), returns the URL;
  the FE opens it.
- `GET /rfp-portal/buildingconnected/callback?code&state`: verifies the
  state, exchanges the code, calls `GET /users/me`, refuses (and stores
  nothing) when `bidBoardPermissions.viewAll` is false ("This Autodesk user
  cannot see the whole Bid Board"), otherwise upserts the connection row
  (`status = connected`) and redirects to `/settings/rfp-ingestion?bc=connected`.
  Unauthenticated by design (the browser arrives from Autodesk); the state
  is the proof. A `state` that is not `token_urlsafe(32)` shaped (43
  characters of `[A-Za-z0-9_-]`) is refused as `rfp_bc_state_invalid`
  before any DB call, and one per-process limiter shared by every caller
  (`RFP_BC_CALLBACK_RATE_LIMIT_PER_MIN`, default 30; off with
  `RATE_LIMIT_ENABLED=false`) answers 429 `rate_limited` beyond it. When
  `RFP_BC_EXPECTED_COMPANY_ID` is set, a `/users/me` `companyId` that
  differs is refused (nothing stored, audited `rfp_bc.connect_refused`
  with reason `company_mismatch`, redirect reason `rfp_bc_not_connected`).
  A connect that replaces a different Autodesk user or company rings IT
  Admins (`rfp_bc.account_changed`, bell only).
- `POST /rfp-portal/buildingconnected/disconnect`: revokes the stored
  refresh and access tokens at Autodesk (`POST /authentication/v2/revoke`,
  client basic auth, best effort: a failure is logged and never blocks),
  then clears the tokens and sets `disconnected`. The automatic
  disconnects (refresh rejected, token refused) make no revoke call.
- `GET /rfp-portal/buildingconnected/status`: connection status, connected
  user, last scan, last full sync, high-water mark, counts, next run.

### 4.4 Requests

`httpx.Client` (HTTP/1.1, the project rule), base
`https://developer.api.autodesk.com/construction/buildingconnected/v2`,
`Authorization: Bearer`, timeout `RFP_BC_REQUEST_TIMEOUT_SECONDS` (60).
`limit = 100` always. 429: sleep `Retry-After` (cap 120 s), retry up to 3
times. 401: refresh once, retry once, then `BcDisconnected`. 5xx: retry
twice with backoff. Every request logs method, path, status, elapsed; never
the token or the body.

`x-bc-mode: test` is honoured when `RFP_BC_TEST_MODE = true` (read-only
Autodesk sample data, useful for the FE without touching the real board).

---

## 5. Data model (migration `0136_rfp_buildingconnected.sql`)

`rfp_portal_invitations` (existing; additions, all nullable so NGEM rows are
untouched):

| column | notes |
|---|---|
| external_id | text, the opportunity id (also stored in `bid_number` for the existing unique key; `agency_key = 'bc'`, `agency = external GC name or '(NDA)'`) |
| external_url | text, the deep link |
| payload | jsonb, the full opportunity as pulled (the mirror) |
| payload_hash | text, sha256 of the canonical JSON, to skip no-op updates |
| bc_updated_at | timestamptz (`updatedAt`, null on masked rows) |
| invited_at | timestamptz |
| job_walk_at, expected_start_at, expected_finish_at, rfis_due_at | timestamptz |
| address | text |
| trade_name | text |
| submission_state, workflow_bucket, source, request_type | text |
| is_archived, is_nda_required, is_sealed | boolean |
| gc_external_id, gc_external_name | text |
| gc_id | uuid references general_contractors on delete set null |
| gc_kind | text check in (`alias`, `contact`, `domain`, `provisional`, `none`) |
| gc_candidates | jsonb `[{gc_id, name, score}]` |
| gc_confirmed_at | timestamptz |
| lead | jsonb `{first_name, last_name, email, phone}` |
| created_project_id | uuid references projects on delete set null (NGEM already has the equivalent through 0130; reuse) |
| ignore_source | text check in (`user`, `system`) |

`status` check widened: `match`, `review_match`, `exists`, `create`,
`created`, `done`, `ignored`, `historical`, `expired`, `withdrawn` (the
NGEM values `harvest` and `split` stay for NGEM). `agency`, `bid_number_raw`,
`title` keep `not null` (filled with the name / id). Index
`(portal, external_id) where external_id is not null`, `(portal, status,
close_at)`, `(gc_external_id)`.

`rfp_portal_runs`: `kind text not null default 'incremental' check in
(`incremental`, `full`)`, `high_water_before timestamptz`, `rows_pulled int`,
`rows_pipeline int`, `rows_expired int`, `rows_withdrawn int`.

`rfp_portal_state` (new): `portal text primary key`, `high_water_at
timestamptz`, `last_full_sync_at timestamptz`, `last_incremental_at
timestamptz`, `updated_at`.

`rfp_oauth_connections` (new): section 4.2 columns; `provider text primary
key`. `rfp_oauth_states` (new): `state text primary key`, `actor_id uuid`,
`expires_at timestamptz`.

`gc_external_aliases` (new): `id uuid pk`, `source text not null`
(`buildingconnected`), `external_id text not null`, `external_name text not
null`, `gc_id uuid not null references general_contractors on delete
cascade`, `confirmed_by uuid references profiles on delete set null`,
`confirmed_at timestamptz not null default now()`, `created_at`,
`updated_at`; unique `(source, external_id)`; index `(gc_id)`.

`projects`: `job_walk_at timestamptz`, `project_information text`,
`trade_instructions text`, `gc_confirm_pending boolean not null default
false`, `files_needed_source text`, `files_needed_url text`,
`files_needed_set_at timestamptz`, `files_needed_cleared_at timestamptz`,
`files_needed_cleared_by uuid references profiles on delete set null`.
`invitation_method` gains `buildingconnected` wherever a check exists.

`llm_jobs.job_type` check gains `rfp_bc_full_sync`. Bell types registered.
View `rfp_created_search` (0135) gains nothing: the GC name, method and
sender already cover BuildingConnected rows.

All new tables: RLS enabled and forced, no policies; `set_updated_at`
trigger; idempotent DDL; `notify pgrst, 'reload schema'` at the end. Applied
to DEV only until release.

---

## 6. API (`app/routers/rfp_portal.py`, additions)

Existing NGEM endpoints work unchanged with `portal = buildingconnected`
(list with lanes, detail, review, resolve exists, ignore, restore, create
project, counts). Additions:

| Method and path | Role | Purpose |
|---|---|---|
| GET /rfp-portal/buildingconnected/connect | IT Admin, Executive | authorize URL |
| GET /rfp-portal/buildingconnected/callback | none (state-verified) | token exchange, redirect |
| POST /rfp-portal/buildingconnected/disconnect | IT Admin, Executive | |
| GET /rfp-portal/buildingconnected/status | review-queue roles | status block |
| POST /rfp-portal/buildingconnected/run | IT Admin, Executive, Estimating Admin | `{kind: incremental|full}` manual run |
| POST /rfp-portal/{id}/gc | writer roles | confirm / pick / create the GC; writes the alias |
| POST /rfp-portal/{id}/apply-dates | writer roles | apply moved dates to the linked project |
| POST /rfp-portal/{id}/process | writer roles | historical -> match |
| GET /gc-aliases, PATCH /gc-aliases/{id}, DELETE | IT Admin | settings list |
| POST /projects/{id}/files-needed/dismiss | writer roles | |
| GET /projects/{id}/gc-confirm, POST | writer roles | the project-page card (reads the linked invitation) |

Lanes on the list: `needs_action` (`review_match` + unconfirmed GC on
`create`), `open` (`match`, `create`), `existing` (`exists`, `created`),
`historical`, `expired_withdrawn`, `ignored`. Filters: state, GC, due range,
search. Each row carries the deep link.

---

## 7. Frontend

- `/rfp-emails`: "BuildingConnected" tab beside NGEM (`RfpPortalTab` with
  `portal = buildingconnected`; new columns GC, trade, invited, due, state,
  flags; the Review dialog gains the GC card; row actions Create project /
  Process / Ignore / Restore / Open in BuildingConnected).
- `/settings/rfp-ingestion`: `RfpBuildingConnectedSection`: Connect /
  Disconnect, connected user and company, last incremental, last full sync,
  next run, counts, "Run now" and "Full sync now", a GC aliases table (name
  in BuildingConnected, BDR GC, confirmed by/at, re-point, delete).
- Project page: the GC confirm card (shown while `gc_confirm_pending`; the
  two names side by side, Yes / Pick another / Add a GC), the files-needed
  banner, the "Also invited through BuildingConnected" RFP block with the
  change list and Apply, "Job walk" in the dates group, "Project details"
  with the two labelled sections (Project information / Trade-specific
  instructions). Edit Details edits job walk and both text fields.
- "Created from RFP Ingestion": method badge `BuildingConnected`, the flags
  `files needed` and `confirm GC`.
- Dashboard intake task line: "Files not pulled from BuildingConnected" and
  "Confirm the GC" when set.
- Six catalogs, English strings (the RFP namespace rule).
- Design system: the neumorphic navy-on-light components, no glass.

---

## 8. Security notes

- Tokens live only in `rfp_oauth_connections` (service role, RLS forced);
  never in logs, responses or the FE. They are plain text in the table
  (no app-level encryption yet: that needs a key management decision).
  Disconnect revokes them at Autodesk first. The client secret stays in env.
- The callback is unauthenticated by necessity; the signed, single-use,
  10-minute `state` binds it to the initiating user. A callback with an
  unknown or expired state is a 400 with nothing stored.
- Only IT Admin and Executive may connect or disconnect; the scan runs as
  the service.
- `payload` is stored verbatim (GC contact data included). The tab and the
  API expose the projected columns, not the payload, except to IT Admin on
  the detail ("Raw record").
- HTML from BuildingConnected is converted to text before storage; the FE
  never renders it as HTML.
- Deep links go to `app.buildingconnected.com` only (validated prefix).
- The API is read-only by scope; even a bug cannot write to Bid Board.

---

## 9. Configuration

| Setting (env) | Default | Notes |
|---|---|---|
| `rfp_bc_enabled` (`RFP_BC_ENABLED`) | false | on top of `rfp_ingest_enabled`; `features.rfp_buildingconnected` |
| `building_connected_client_id` / `_secret` | set | existing names; `BUILDING_CONNECTED_ENABLED` becomes an alias of `RFP_BC_ENABLED` |
| `rfp_bc_redirect_url` | derived from the backend public URL | must match the APS app |
| `rfp_bc_poll_minutes` | 15 | |
| `rfp_bc_full_sync_time` | 02:30 | Pacific |
| `rfp_bc_overlap_minutes` | 30 | |
| `rfp_bc_expire_days` | 7 | |
| `rfp_bc_missing_days` | 3 | |
| `rfp_bc_text_max_chars` | 20000 | |
| `rfp_bc_request_timeout_seconds` | 60 | |
| `rfp_bc_test_mode` | false | `x-bc-mode: test` |
| `rfp_bc_scan_queue_priority` | 140 | shared with NGEM's value |
| `rfp_bc_full_sync_queue_priority` | 150 | the full sync queues behind incrementals |
| `rfp_bc_full_sync_catchup_hours` | 4 | the full slot is retried inside this window |
| `rfp_bc_auto_resolve_enabled` | true | confident match with a CONFIRMED GC resolves to `exists` without a reviewer |
| `rfp_bc_max_pages` | 200 | a pull that hits the cap is recorded as truncated: no missing marking, no high-water move |
| (Settings) `hide_input_in_errors` | true | pydantic never echoes env values (secrets) into a validation refusal; added for the whole model |

Bounds (validator `_validate_rfp_bc`): poll minutes 5..60 and a divisor of 60,
catch-up 1..23 h, overlap 0..1440 min, expire and missing days 1..365, text
cap 1000..20000, timeout 0 < t <= 600, pages 1..10000. In production the
slice refuses to boot with `RFP_BC_TEST_MODE=true` or, when enabled, without
an https `RFP_BC_REDIRECT_URL`.

Release needs 0136 on staging and prod, the callback URL registered for each
backend host, and the Railway vars, all with explicit approval
(`never-touch-prod-without-approval`).

---

## 10. Tests

- `test_bc_client.py`: pagination, filter formatting, 429 with Retry-After,
  401 -> refresh -> retry, refresh rotation write-before-use, lock
  contention between two workers (one refresh, the other waits and reads the
  new token), `invalid_grant` -> disconnected + bell.
- `test_rfp_bc_portal.py`: entry rule over the fixture set (anonymised rows
  from the live pull: masked NDA row, no due date, past due, declined,
  archived, foreign row without `clientValues`, Electrical + Low Voltage pair,
  cross-GC pair, rebid pair), change tracking and the Apply path, expiry and
  withdrawal, full sync marking, high-water mark only advancing on success.
- `test_gc_aliases.py`: alias, contact, domain, provisional, none; confirm
  Yes / pick / create; re-point; name drift; project link swap rules.
- `test_rfp_create_bc.py`: facts mapping incl. every null, HTML to text,
  budget suffix, lead contact creation, flags.
- Router tests for roles on every new endpoint and for the callback state.
- FE: lint, tsc, headless screenshots of the tab, the settings block, the
  project card and banner.

---

## 11. Out of scope (this slice)

Webhooks (`opportunity.created` / `status.updated`), comments, Bid Board
analytics from the mirror (outcomes and competitors are unused on this
board anyway), writing anything back (status, decline, bids), Autodesk Docs
file linking, scraping the web app for files, TradeTapp, NGEM changes.

---

## 12. Open items

1. GC name scoring against the real GC list needs prod's
   `general_contractors` names (dev holds 11 test rows). A read-only SELECT
   on prod requires the user's approval; until then the alias flow is designed
   to work at any score.
2. The exact backend public URL per environment for the callback.
3. Whether `updatedAt` moves when a GC edits a date cannot be proven from a
   snapshot; the nightly full sync covers the case either way.

---

## 13. Corrections after the code map (2026-09-26, before the build)

Seven read-only agents mapped the code against sections 1 to 12. The build
contract (`scratchpad/BUILD_CONTRACT.md` for the build session; its decisions
are repeated here) overrides the earlier sections where they disagree:

- No `projects.invitation_method` column exists or is added;
  `rfp_created_projects.invitation_method = 'buildingconnected'` is the
  record. No `is_budgetary` column: the B suffix is the `budgetary` argument
  of `project_numbers.insert_with_assigned_number`, threaded through
  `rfp_create._insert_project`.
- One queue job type (`rfp_portal_scan`); the run row's new `kind` column
  says incremental or full. `llm_jobs_job_type_check` is untouched.
- The slot index `rfp_portal_runs_slot_uidx` becomes unique on
  `(portal, kind, scheduled_for)` so the 02:30 full sync and the 15-minute
  grid never collide. Incremental slots are `floor(now, 15 min)` and only
  the newest due slot is claimed (no catch-up burst); the full sync has a
  4-hour catch-up window.
- `rfp_created_projects.gc_plan` check is widened with `provisional`.
- With an unconfirmed GC the matcher parks EVERY confident match at
  `review_match` (`match_gc_unresolved`), not only ambiguous ones. This is
  accepted: the reviewer confirms the GC there, and confirming re-runs the
  match automatically. `auto_merge` comes from a new
  `RFP_BC_AUTO_RESOLVE_ENABLED` (default true), not the email switch.
- `exists` with a GC new to the project inserts a plain `project_gcs` link
  (`rfp_match_id` null, id kept in `rfp_portal_invitations.project_gc_id`);
  `rfp_project_matches` is not touched, so there is no Merged-by-System
  badge or unmerge for BuildingConnected links. `reopen` removes the link by
  id, refusing when a proposal was sent.
- Same-GC package pairs (Electrical + Low Voltage) and same-scan siblings
  are linked to one project by a scan-time sibling rule and a create-time
  guard keyed on (GC, normalized name); the matcher alone cannot do it.
- The files flag clears on any `DRAWING_CATEGORIES` member or a
  specification, from every insert path (upload, promotion, split, draft
  materialize), best effort.
- Apply-dates is limited to ACTUAL_BID_EDITOR_ROLES; change values are shown
  only to ACTUAL_BID_VIEWER_ROLES. Run now is IT Admin, Executive, Estimating
  Admin. The BC tab is hidden from the accountant.
- Lanes: needs_action (review_match), open (match, create, done), existing,
  historical, expired_withdrawn, ignored, all. One `restore` action serves
  historical, expired and withdrawn.
- Redirect URL is an explicit setting `RFP_BC_REDIRECT_URL`; local value
  `http://localhost:5051/rfp-portal/buildingconnected/callback` (must be
  registered in the APS app).
- `rfp_bc.disconnected` goes to IT Admins and Executives and mirrors to
  email; the other three bells do not.
- Text fields keep their newlines (a dedicated `html_to_text`), never
  `rfp_create._text`.


---

## 14. Build record and live run (2026-09-26, dev database)

Built in one session by ten implementers on disjoint file sets against the
build contract (section 13), then a five-lens adversarial review (36 raw
findings, 29 confirmed by three refuters each, 15 mediums and the one high
fixed in round one, the rest in round two), then the live run below.

**Code.** New: `app/services/bc_client.py` (OAuth store, refresh lock,
retries, paging), `bc_facts.py` (pure projection of an opportunity, HTML to
text, entry rule, tracked changes, project facts), `gc_aliases.py`,
`rfp_bc_portal.py` (the portal object: scan, sibling index, refinements,
on_exists, expiry and withdrawal, status, confirm_gc, apply_dates),
`files_needed.py`, `app/routers/rfp_bc.py` (OAuth, status, run, aliases; the
callback on its own unauthenticated router). Edited: `rfp_portal_ingest.py`
(portal hooks, statuses, lanes by portal, claim_slot kind, poll_once per
portal, restore, run_now kind, BC selects, portal_sources for created rows),
`rfp_create.py` (Facts widened, `facts_for_bc`, `GC_PROVISIONAL`,
`ensure_lead_contact`, budgetary threading, `apply_project_dates`,
`swap_project_gc`), `routers/projects.py` (files-needed dismiss, gc-confirm,
new PATCH fields), `routers/rfp_created.py` (flags), `routers/rfp_portal.py`
(gate by portal, new actions, BC date redaction for non viewers, harvest
refused on BC rows), `routers/rfp_processing.py` (any portal), `config.py`,
`features.py`, `main.py`, `llm_queue.py`, `notification_email.py`, the four
file-insert hooks. Frontend: BuildingConnected tab and modal with the GC
card, settings section with Connect and the alias table, project banner and
GC confirm card, Job walk and Project details, Created-page flags, dashboard
chips, bell routes, 317 English keys in all six catalogs, `npm test`
(node:test) with 12 tests over the pure view helpers.

**Tests.** Backend 5,414 green (`tests/test_llm_routing.py` excluded: it
hangs on the stopped self-hosted box, unrelated to this slice); new files
`test_bc_client.py`, `test_bc_facts.py`, `test_gc_aliases.py`,
`test_rfp_bc_portal.py`, `test_rfp_create_bc.py`, `test_rfp_bc_router.py`,
`test_rfp_bc_config.py`, `test_migration_0136.py`; fixtures
`tests/fixtures_bc/` (24 anonymised real rows). Frontend eslint 0 errors,
tsc clean.

**Migration 0136 applied to DEV only** (bpidntbyvoooqvaispup, 2026-09-26).

**Live run on dev (in-process, bench session left untouched):** the
connection was seeded from the spike's tokens (Thomas Moore, view all). Full
sync: 26 pages, 2,543 pulled, 40 entered the pipeline, 2,503 historical,
NDA rows carry agency "(NDA)", high water moved only on completion, one
`rfp_bc.new_invitations` bell per review-role user. The first sync took
569 s because every unchanged row got its own "seen" update; after the fix
(one select and one chunked `last_seen` touch per API page, set-difference
missing marking in chunks of 200, lazy sibling index) a full re-sync of the
same 2,543 rows took 92 s (20 s of it the API) and an incremental scan 7 s.
`seen_count` now counts the pulls that wrote a row, not every pull that saw
it; `last_seen_at` and `missing_since` stay exact. The GC-confirm swap also
removes the lead contact it filed under the provisional GC when nothing
references it and it was created with the project. Sweep, one tick of 16 s: the 21 rows with a due date drained
to `done` (button-first), the 19 without one parked at `review_match` /
`no_due_date`; every GC provisional (dev holds test GCs only). Creation
from the row "CITI Bank Las Vegas Tropicana TI": project 26.9.7126 with the
deep link, address, `actual_bid_at`, the provisional GC linked with
`needs_by`, `gc_confirm_pending`, `files_needed_*` set, notes "Created from
BuildingConnected: AAA FACILITY SERVICES, package Electrical, invited
2026-09-19. <link>", `sender_display` "Nancy Lopez <...>", `gc_plan`
provisional; the row moved to `created`.

**Known and deferred.** The BuildingConnected UI is reachable only where
`features.rfp_email_ingest` is on (the RFP pages' gate; both are on for this
company). BuildingConnected links on `exists` are plain `project_gcs` rows
(no Merged-by-System badge). The GC-confirm card on a project with an
unconfirmed match candidate acts only on rows that created the project.
Release needs 0136 on staging and prod, the callback URLs registered per
host, and the Railway vars (`RFP_BC_ENABLED`, `RFP_BC_REDIRECT_URL`,
`BUILDING_CONNECTED_CLIENT_ID/_SECRET`), all with explicit approval, plus one
Connect click by a view-all user per environment.

**UI smoke (headless Playwright, magic-link cookie injection, 1440 px).**
Every surface rendered with data and without raw i18n keys or console
errors: the BuildingConnected tab (lanes needs action 19, open 20, existing
1, historical 2503), the invitation modal (facts, GC card, Open in
BuildingConnected, Create project on `done` rows, Process on historical
rows), the settings block (connected as Thomas Moore, view all, runs, lane
counts, aliases table for IT Admin only), the created project (files-needed
callout, GC confirm card, Job walk, the two-block details section, the RFP
block), the Created page flags and the dashboard chips. Twelve presentation
defects were found and fixed the same day: the Project column clipped at
1440 px (tighter BC columns, visible scrollbar), stale rows during a lane
switch (rows dim under the spinner, in-flight requests aborted), NGEM
wording "Match to confirm" on BC review rows (now "Needs review" plus the
reason, no duplicated "No due date"), raw `workflow_bucket` and "decided at
match" in the modal, a matcher conflict object rendered as JSON (now a
sentence; this also improves the email drawer), "No due date in the email"
on BC candidates, weak GC guesses (below 0.5) presented as the primary
action (now "No close match in BDR for <name>", Pick / Add primary), the
project RFP block naming the guess instead of the BuildingConnected company,
the bidding URL overflowing the details grid, Bid notes repeating the
trade instructions, and the settings block hiding that the RFP testing
bench pauses scheduled scans (status now carries `paused_by_test_session`;
"Connected by: Seeded" when the connection was seeded by script). Lane
counts went from one HEAD per lane (7 requests, 1.6 to 3.3 s per list
call) to one paged status read (3 requests). Left as is: the empty-value
dash the whole details grid uses (a pre-existing convention), the Go/No-Go
panel's older strings, and the modal facts list showing both "Due: No due
date" and the flag reason.

A second screenshot pass confirmed all of the above. A last polish round
then withheld the "(BDR: X)" label wherever the GC is an unconfirmed weak
guess (modal subtitle, Opportunity GC row, the project's RFP invitations
list), added a "GC unconfirmed" chip beside the GC name in the project
header while `gc_confirm_pending` is set, hid the stale flag reason on
created and exists rows, and made long lead emails break at the "@" instead
of mid-word. Frontend node tests: 19.

**Prod GC list versus the board (read-only prod query, approved 2026-09-27).**
Prod holds 39 real GCs with 40 contact domains. Run through the app's own
resolver, 21 of the board's 295 BuildingConnected companies resolve
CONFIRMED through their lead-email domain and will never prompt: Burke (184
rows), Rafael (153), Builders United (74), Shaw-Lundquist (60), CORE (44),
DC Building Group (40), Parkway (33), Eagle One (32), Taylor International
(25), Tre Builders, Catamount, Metcalf, OS Construction, New-Com, Signal
Hill, Showcase, Boyd Martin, Wells, MS Commercial (Mark Scott), The Monument
Company, Martin-Harris. Of the 25 companies behind the 40 rows the entry
rule admits today, 11 are domain-confirmed and 14 need one confirmation each
(most are GCs not in BDR yet: McCarthy, Westland, Dante, Austin, ORO, G.E.,
TB Penick, DAVACO, North Georgia Civil, Dedicated, BBCH, Construction One,
B&H, AAA Facility Services). Eleven prod GCs never appear on the board (they
invite by other channels). Data note for the user: prod has two "DNI
Construction" rows sharing dniconstruction.com, so a DNI invitation would not
domain-resolve until the duplicate is merged. Scoring note: single shared
generic tokens produced false strong scores (United Construction Company vs
Builders United 1.00; four pairs at exactly 0.50 through one shared word);
the provisional score now ignores a lone generic token (see the test pairs
in test_gc_aliases.py).

**Fix round 3 (2026-09-28, code only, no migration; `rfp_bc_portal.py`,
`bc_facts.py`).** Three defects from the live tests on dev. (1) A
`review_match` row parked `no_due_date` now goes back to `match` (flag
cleared, `decided_at_step` null, the usual reset) when the board's due
date goes from null to a value. (2) A GC swapped on the board is no longer
silent: `gc_external_id` and `gc_external_name` are tracked fields (change
log entries; the scan select now reads `gc_external_name`, `gc_kind`). A
new company id on an UNLINKED row clears `gc_id`, `gc_kind`,
`gc_candidates`, `gc_confirmed_at`, `gc_confirmed_by` (fenced on no
project) and sends a `review_match`, `create` or `done` row back to
`match`, where the sweep resolves the new company (a row at `match` just
loses its resolution; ignored and parked rows keep their status; a
declined or archived swap is not re-matched). On a LINKED row the project's
GC and the row's `gc_id` / `project_gc_id` are never rewritten:
`projects.gc_confirm_pending` goes true, the row's GC becomes an
unconfirmed guess (`provisional`, confirmation cleared, candidates
emptied), and `rfp_bc.invitation_changed` rings with "GC changed: <old> to
<new>" (`gc_external_id` joins `BELL_FIELDS`). A rename of the same
company (`gc_external_name` alone) is logged only. `confirm_gc` now also
clears the pending flag when answered on the row whose own board swap
raised it, unless a creator row's GC question is still open. (3) A second
change while an Estimating Admin's `invitation_changed` bell for the same
invitation is unread rewrites that bell in place (message, metadata with
the latest `fields` and `run_id`, `created_at` moved to now) instead of
ringing nobody; users without an unread bell get a new row. Tests: 150 to
157 in the three BuildingConnected files. Open for other files: the
frontend catalogs need `changeLog.fields` / `changes.fields` labels for
`gc_external_id` and `gc_external_name`, and the project GC card
(`routers/projects._bc_invitation_for_project`) still prefers the creator
row over a merged row that raised the question.

**Fix round 4 (2026-09-28, code only, no migration; with the
`gc_aliases.py` / `rfp_create.py` round).** "Yes, same GC" on the creator
row now goes through `swap_project_gc` with the GC unchanged, since creation
no longer files the lead under a provisional GC. `gc_alias` change-log
entries (alias repoint or delete) count like a board swap for the pending
flag (`rfp_bc_portal.open_gc_question_at`: the newest `gc_external_id` or
`gc_alias` entry newer than `gc_confirmed_at`). `_step_match` fences its
write on the `gc_external_id` the tick read. The project GC card
(`routers/projects._bc_invitation_for_project`) serves the linked row with
the newest open question (`rfp_bc_portal.card_invitation`) before the
creator row. Labels for `gc_external_id`, `gc_external_name` and `gc_alias`
in `_FIELD_LABELS` and in all six frontend catalogs (English text).
Sections 1, 3.4, 3.6 and 3.7 now state the 0.5 provisional floor, the
confirmed-only lead contact, the board GC change handling and the bell
refresh in place.
