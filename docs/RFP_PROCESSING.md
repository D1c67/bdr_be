# RFP Processing page

Status: contract written 2026-09-23. Dev only, code only, NO migration.

The `/rfp-processing` page is the operations view over the RFP intake
pipeline: every email row and portal invitation that is still in flight, with
the rows that wait on a person (the three human lanes) and the rows that are
stuck (failed, retrying, waiting on the model, or stalled) surfaced first.
It complements `/rfp-emails` (the tabbed review queue, RFP_EMAIL_INGESTION.md
section 8) rather than replacing it: same rows, same detail dialog, same
actions, plus a review prompt that pushes the decision at the user.

## 1. Decisions (2026-09-23)

- New page `/rfp-processing`, nav label "RFP Processing", listed right after
  "RFP emails" in the sidebar. Same gate as `/rfp-emails`: feature flag
  `rfp_email_ingest` and the RFP VIEW roles (Estimating Admin, Executive,
  IT Admin, plus the read-only Accountant). Accountant sees everything the
  page shows and gets no action control (every action is 403 server side).
- Rows are scoped exactly like `/rfp-emails` (RFP_EMAIL_VISIBILITY.md):
  `rfp_email_visibility.apply_scope` on every email read and count. Portal
  invitations are not mailbox rows; they are visible to the review roles
  whenever the backend serves `rfp_ngem`, as on the NGEM tab today.
- "In flight" = email statuses `STATUS_PENDING + STATUS_HUMAN` plus `failed`
  rows updated within the last 14 days; portal statuses `match, harvest,
  split, create, review_match`. Everything else is finished and belongs to
  the other pages.
- Lanes (the buttons on the page, each with a live count):
  - `review_llm` "Classification review": the model was unsure, a person
    answers yes or no.
  - `flagged_unauthorized` "Unauthorized sender": allow and continue,
    dismiss, add a sender rule, or block the sender.
  - `review_match` "Match review": merge into an existing project, record as
    a duplicate, or continue as a new project. Email rows and portal
    invitations both land here.
  - `processing`: automated steps running normally.
  - `stuck`: see section 3. A row is in exactly one lane; the three review
    lanes and `stuck` win over `processing`.
- Awaiting-review rows are tinted amber with an "Awaiting review" flag badge
  so they stand out from processing rows; stuck rows are tinted red with a
  "Stuck" badge naming the reason. Processing rows are plain.
- Opening a row in a review lane opens the row's detail dialog and, on top
  of it, the review decision sub-dialog at once. The user can close the
  sub-dialog (click outside or "Decide later") and read the detail; an
  illuminated "Review" button in the detail dialog header reopens it. The
  Review button is present whenever the row is in a human lane, on both
  `/rfp-processing` and `/rfp-emails`. The automatic prompt fires only when
  opened from `/rfp-processing` (`promptReview`), never on `/rfp-emails`.
- The decision controls in the sub-dialog are the SAME calls the detail
  footer makes today (review yes/no, continue/dismiss, match merge/duplicate/
  reject, portal resolve/new/ignore). No new decision semantics.
- Stuck rows get one action, "Retry": a `failed` email row goes back to the
  step that failed with a fresh attempt budget; a retrying or model-waiting
  email row is made due now. Portal invitations have no retry here (they have
  reopen/ignore on their own modal).
- No em dashes anywhere (CLAUDE.md).

## 2. API

Router `app/routers/rfp_processing.py`, prefix `/rfp-processing`, mounted in
`main.py` beside `rfp_emails` with the same fail-closed flag gate (404 while
the email intake is not served) and the same rate limiter pattern. View
routes use `require_view_queue` (VIEW roles), the retry uses
`require_review_queue` (REVIEW roles), both imported from `rfp_emails`.

### 2.1 `GET /rfp-processing/summary`

```
{
  "lanes": {"review_llm": n, "flagged_unauthorized": n, "review_match": n,
            "processing": n, "stuck": n, "review_total": n, "total": n},
  "steps": {"received": n, "auth": n, "keywords": n, "classify": n,
            "authorize": n, "method": n, "extract": n, "match": n,
            "harvest": n, "split": n, "create": n},
  "stuck_kinds": {"failed": n, "retrying": n, "model_wait": n, "stalled": n},
  "portal_served": bool,
  "generated_at": iso
}
```

`steps` counts processing rows only (email + portal, by status).
`review_total` = the three review lanes summed. `total` = every in-flight
row the viewer can see. Counted inside the viewer's mailbox scope (a viewer
with nothing in scope gets zeros for the email side without a query).

### 2.2 `GET /rfp-processing?lane=all&limit=50&offset=0`

`lane` in `all | review | review_llm | flagged_unauthorized | review_match |
processing | stuck` (`review` = the three review lanes together). Answers
`{items, total, offset, limit, lane}`.

Implementation: read every in-flight email row the viewer can see (one
scoped select, `_LIST_SELECT` columns plus `attempts, next_attempt_at,
last_error, updated_at, decided_at_step`, paged at 1000 and
capped at 2000 rows) and every in-flight portal row when `rfp_ngem` is
served, classify each in Python (section 3), filter by lane, sort, then
slice `offset/limit`. The in-flight set is small by construction (finished
rows never load), so the Python union is fine; document the cap.

Sort order: review rows first, then stuck, then processing; inside a group
the OLDEST `received_at` (portal: `invited_at` or `created_at`) first, so
the row that has waited longest leads.

Item shape (one shape for both sources; absent facts are null):

```
{
  "source": "email" | "portal",
  "id": uuid,
  "status": str,
  "lane": "review_llm" | "flagged_unauthorized" | "review_match" | "processing" | "stuck",
  "awaiting_review": bool,
  "stuck": null | {"kind": "failed" | "retrying" | "model_wait" | "stalled",
                   "step": str, "attempts": int, "max_attempts": int,
                   "next_attempt_at": iso | null, "last_error": str | null,
                   "since": iso},
  "name": str | null,            // extracted_project_name, or portal title
  "subject": str | null,
  "from_address": str | null, "from_name": str | null,
  "gc_name": str | null,         // resolved GC name, else extracted_gc_name
  "invitation_method": str | null,   // portal rows: "ngem"
  "received_at": iso, "updated_at": iso,
  "attempts": int, "next_attempt_at": iso | null, "last_error": str | null,
  "harvest_status": "pending" | "running" | "complete" | "failed" | null,
  "match_project": {"id", "name", "number"} | null,
  "created_project_id": uuid | null,
  "primary_mailbox": str | null,
  "portal": "ngem" | null
}
```

`last_error` is the row's own text, already capped at 500 chars by the
pipeline; it never contains mail bodies. `match_score` is NOT in the item
(it may carry the actual-date bucket, see the list route's docstring).

### 2.3 `POST /rfp-processing/emails/{email_id}/retry`

REVIEW roles. Loads the row through `rfp_emails._email_row_or_404(..., user=)`
(404 on invisible rows). Service function `rfp_email_ingest.retry_row(sb,
email_id, user_id)`:

- `failed` row: target = `decided_at_step` when it is in `STATUS_PENDING`,
  else `LookupError` (409 `rfp_processing_not_retryable`). CAS from `failed`
  to the target with `attempts 0, last_error None, next_attempt_at None,
  flag_reason None, decided_at_step None`.
- pending row with `attempts >= 1` or a set `next_attempt_at` (retrying or
  model wait): CAS keeping the status, `next_attempt_at None` (due now),
  attempts untouched.
- any other status: `LookupError` (409). Sibling followers (a row whose
  leader is another row, per the dedup rule in rfp_match) are refused the
  same way.
- Audit `rfp_email.retry` with `{from_status, to_status, attempts}` through
  the router's existing audit helper. Answers the detail (`_detail`).

Concurrency: the CAS pattern used everywhere in the service (`_cas`); a
lost race answers 409 like the other actions.

## 3. Lane and stuck classification

Pure function `classify_row(row, *, now, source, max_attempts,
stall_minutes, slow_stall_minutes, harvest_status)` in
`app/services/rfp_processing.py` returning `(lane, stuck | None)`.

1. status in `("review_llm", "flagged_unauthorized", "review_match")`: lane
   = that status, stuck = None (a person is the step; never stuck).
2. email status `failed`: stuck `failed`, step = `decided_at_step`.
3. pending status and `attempts >= 1`: stuck `retrying` (n of max).
4. pending status, `attempts == 0`, `last_error` set and `next_attempt_at`
   set: stuck `model_wait` (the model is away; nothing spent). Exception
   (2026-09-23 live finding): when `last_error` is the pipeline's own
   sibling wait sentence ("Waiting for another copy of this message ...",
   older rows "Waiting for an earlier copy ..."), the row is a later copy
   parked at `extract` behind an older copy and follows that copy's
   decision; it is `processing`, not stuck, and the page shows the sentence
   in its Detail cell.
5. pending status and `updated_at` older than the stall threshold with
   `next_attempt_at` null or past: stuck `stalled`. Threshold =
   `RFP_PROCESSING_STALL_MINUTES` (default 30) for the quick steps;
   `RFP_PROCESSING_SLOW_STALL_MINUTES` (default 360) for `harvest` and
   `split`. A `harvest` row whose harvest job is `running` or `pending`
   is never stalled (the job is the progress).
6. otherwise lane `processing`.

`since` = `updated_at` for stalled and failed, `next_attempt_at` is echoed
for retrying and model wait. Both settings are new `Settings` fields in
`app/core/config.py`, documented in section 6, no env change needed.

## 4. Frontend

- `lib/rfpProcessing.ts`: lane vocabulary, item and summary types, fetchers
  (`fetchRfpProcessingSummary`, `fetchRfpProcessing`, `retryRfpProcessingEmail`),
  `rfpProcessingHref(lane?)`, `RFP_PROCESSING_ROLES = RFP_EMAIL_VIEW_ROLES`.
- `components/RfpEmailDetailModal.tsx`: the `DetailModal` moved out of
  `app/(app)/rfp-emails/page.tsx` unchanged in behavior (with its helpers
  `StatusBadge`, `ModelVerdict`, `KeywordChips`, `InertBody`, `BlockSenderModal`
  and friends moved with it or into a shared module the page imports), plus:
  - prop `promptReview?: boolean`;
  - a "Review" button in `Modal.Header` shown whenever `detail.status` is a
    human lane and `canAct`: variant primary with an attention ring (a
    pulsing ring utility, kept in the UI kit's neumorphic navy style, no
    glass) so it reads as illuminated;
  - `RfpReviewDecisionModal` (new file `components/RfpReviewDecisionModal.tsx`)
    rendered on top when open. Opens automatically once per dialog open when
    `promptReview` and the loaded status is a human lane; reopens from the
    Review button; closes on outside click, Escape, or "Decide later".
- `RfpReviewDecisionModal` content by status, all through the detail
  dialog's existing `act` helper so 409 handling, refresh and `closeAfter`
  stay identical:
  - `review_llm`: subject, sender, `ModelVerdict` + reasoning, keyword chips,
    the question "Is this an RFP?", buttons "Not an RFP" / "Yes, it's an RFP".
  - `flagged_unauthorized`: sender and domain, model verdict, buttons
    "Dismiss" / "Allow and continue", ghost "Block sender" (opens the existing
    block dialog), link "Add sender rule" when `canManage`.
  - `review_match`: `RfpMatchSection` + `RfpMatchFooterActions` (the existing
    components, same props), so merge / duplicate / new project / GC pick are
    the same controls as the detail body.
- `components/RfpPortalInvitationModal.tsx`: same `promptReview` prop and
  header Review button for `review_match`; the sub-dialog lists the
  candidates with "Merge into" (existing `onResolve`), "New project" and
  "Ignore" (the existing confirm flows), and "Decide later".
- `app/(app)/rfp-processing/page.tsx`:
  - lane buttons across the top: All, then the three review lanes grouped
    under an "Awaiting review" caption (amber accent, count badge), then
    Processing and Stuck (red accent when > 0). The active lane is pressed;
    `?lane=` in the URL; counts from the summary, polled every 30 s, plus a
    refresh after every action.
  - a compact step strip under the buttons: one chip per automated step
    with its processing count, in pipeline order, zero chips dimmed.
  - the table: Project (name, subject beneath), GC, Source (method or NGEM),
    Progress (status badge; harvest shows the job state; beneath it a
    per-row meter, `components/RfpProgressMeter.tsx` over
    `rfpProcessingProgress` in `lib/rfpProcessing.ts`: one segment per step
    of the row's pipeline, 11 for an email and 4 for a portal invitation
    from `match`, passed steps navy, the current step navy pulsing while
    processing, amber in a review lane at the step that handed it over
    (classify / authorize / match), red when stuck at the stuck step, with a
    "Step n of N: name" caption), Waiting since (age +
    timestamp), Detail (the model verdict for review_llm, the best candidate
    for review_match, the stuck reason with next attempt for stuck rows),
    Action ("Review" primary illuminated for review rows; "Retry" for
    retryable email rows; nothing otherwise). Rows: amber tint + flag badge
    for review, red tint + "Stuck" badge for stuck, plain otherwise. Clicking
    a row opens its dialog (`?email=` / `?invitation=` in the URL like the
    queue page).
  - list polled every 30 s while open; empty states per lane; paging 50.
- Sidebar: `RFP_PROCESSING_NAV` after `RFP_EMAILS_NAV`, same roles and flag;
  badge = `review_total + stuck` from the summary, danger tone when
  `stuck > 0`. `RfpEmailsActivityProvider` gains `processing:
  RfpProcessingSummary | null` polled on the same 30 s cadence under the same
  enable rule, and `refreshCounts` re-reads it.
- i18n: `nav.rfpProcessing` and a `rfpProcessing.*` block in all six
  catalogs (English in every catalog, like the rest of the RFP namespace).
  Any new key used by the moved detail dialog stays under `rfpEmails.*`.
- Lint gates (BDR FE Next 16): no synchronous setState in effects, no ref
  reads in render; `npx tsc --noEmit` and `npx next lint` must be clean.
  Never run `npm run build` beside the running `next dev`.

## 5. Security notes

- Every email read goes through `apply_scope`; every per-row route through
  `_email_row_or_404(..., user=)`. Invisible rows are 404, never 403.
- Nothing new renders HTML: names, subjects and errors are text. The list
  never carries bodies, previews, links or `view_url`.
- Retry is REVIEW roles only and is a CAS; it cannot move a row that is not
  `failed` or waiting, and never touches terminal rows other than `failed`.
- Rate limited like `/rfp-emails`.

## 6. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `RFP_PROCESSING_STALL_MINUTES` | 30 | pending row with no progress for this long is "stalled" |
| `RFP_PROCESSING_SLOW_STALL_MINUTES` | 360 | same, for `harvest` and `split` |

No new feature flag: the page follows `rfp_email_ingest`.

## 7. Tests

Backend `tests/test_rfp_processing.py` (FakeDB from
`tests/test_rfp_email_ingest`): route table and gates (404 flag off, 403
roles, Accountant reads but 403 on retry), `classify_row` for every rule in
section 3 (incl. the harvest-running exemption and both thresholds), the
summary counts and scope (a viewer with nothing in scope gets zeros and no
email query), the list lanes, sort order and paging over the union, the
item shape (no `match_score`, no body fields), and `retry_row` (failed to
step, waiting to due now, 409 on done/created/review rows, sibling follower
refused, lost CAS race 409, audit row).

Frontend: `tsc` + `next lint` clean; the queue page keeps working with the
extracted dialog (same props, same behavior).

## 8. Build record

(filled by the builders)

### Backend, 2026-09-23 (code only, no migration, uncommitted)

Files:

- `app/services/rfp_processing.py` (new): `classify_row` (section 3, pure),
  the in-flight readers (`load_email_rows` through `apply_scope`,
  `load_portal_rows`), `classify_all`, `summarize`, `select_lane`,
  `email_item` / `portal_item`, the chunked harvest-status lookup.
- `app/routers/rfp_processing.py` (new): the three routes of section 2,
  router gate `rfp_emails.require_rfp_emails`, reads `require_view_queue`,
  retry `require_review_queue`, catch-all rate limiter built the same way as
  `rfp_emails_rate_limit`. Reuses `_LIST_SELECT`, `_email_row_or_404`,
  `_detail`, `_uuid_or_404`, `_projects_by_id`, `_gcs_by_id` and
  `_harvest_status_by_id` from `rfp_emails` by import (nothing duplicated,
  nothing widened there).
- `app/services/rfp_email_ingest.py`: `retry_row` and `_parked_behind_sibling`
  beside `set_project_name`.
- `app/core/config.py`: `rfp_processing_stall_minutes = 30`,
  `rfp_processing_slow_stall_minutes = 360`, next to the classify settings.
- `app/main.py`: `app.include_router(rfp_processing.router)` beside
  `rfp_emails`.
- `tests/test_rfp_processing.py` (new): 75 tests.

Reads per request: one scoped `rfp_emails` select (the in-flight set is one
PostgREST or-group: every pending and human status, or `failed` with
`updated_at` inside 14 days, the timestamp as a Z literal), paged at 1000
and capped at 2000 per source, oldest received first; one
`rfp_portal_invitations` select (when included); one `rfp_harvests` lookup
for rows at `harvest` (the exemption); then, for the list only, one
`projects` and one `general_contractors` lookup over the page. The summary
selects only the classifier's columns. Past the cap a warning is logged and
the counts under-report.

Deviations and readings:

1. The item carries three extra keys: `llm_answer`, `llm_confidence`,
   `llm_reasoning` (null on portal rows). Section 4 asks the Detail column
   to show the model verdict for `review_llm` rows and the 2.2 shape had no
   field for it; these are the same list columns GET /rfp-emails already
   serves to the same roles. Purely additive.
2. Portal rows are included only when the NGEM slice is served
   (`rfp_ingest_enabled and rfp_ngem_enabled`, the `rfp_ngem` flag of GET
   /features) AND the viewer is a REVIEW role. Section 1 says portal rows are
   "visible to the review roles"; the accountant is a VIEW role and the
   invitation dialog's routes (/rfp-portal) are review-only, so the
   accountant could not open one. `portal_served` in the summary therefore
   means "portal rows are part of THIS viewer's counts" (false for the
   accountant even when NGEM is on).
3. Portal `received_at` is `first_seen_at`, else `created_at`: the table has
   no `invited_at` (0126). Portal `gc_name` is null (the agency is the owner,
   not a GC); `invitation_method` and `portal` are the row's `portal` value.
4. `stuck.since` is `updated_at` for every kind (for retrying and model wait
   that is the instant the retry or wait was written); `stuck.next_attempt_at`
   is always echoed.
5. The harvest exemption reads the linked `rfp_harvests.status`. A row at
   `harvest` with no harvest record yet is NOT exempt (it can still stall
   after the slow threshold), although its item's `harvest_status` reads
   `pending`, the queue's display convention. The harvest step re-parks the
   row every poll while the sweep runs, which refreshes `updated_at`, so a
   live pipeline never shows it as stalled.
6. Sibling followers refused by retry: `flag_reason = sibling` (the
   predicate `set_project_name` uses), plus a row at `extract` that the live
   leader rule (`_sibling_leader`, i.e. `rfp_match.choose_sibling_leader`)
   puts behind an OLDER copy that has NOT decided yet (retrying it would only
   make it wait again). Only `extract` waits on a copy
   (`_sibling_short_circuit`), so a copy retrying at any other step, match
   included, is not refused (review fix 2026-09-23). A pending row whose
   leader HAS decided is allowed: its next tick follows that leader, which
   is what a retry is for.
7. Audit: `rfp_email.retry` `{from_status, to_status, attempts}` is written
   by the service (`retry_row`), with the same `notifications.audit` helper
   the router uses, right after the CAS wins, like every other human action
   on these rows (review, continue, dismiss, set name). So it is recorded
   once and never for a lost race. The test bench also gets its
   `human.retry` event for test rows.
8. The 409 code `rfp_processing_not_retryable` is a constant in the router
   (`CODE_NOT_RETRYABLE`), like the create codes on rfp_emails, not an
   `ErrorCode` member. Every refusal (wrong status, failed at a step that is
   not a pending step, sibling follower, lost race) uses it, each with its
   own sentence in `detail`.
9. A `failed` row with `decided_at_step` outside `STATUS_PENDING` (null, or
   a non-step value) is refused as the contract says.

Tests: `tests/test_rfp_processing.py` 75 passed; with
`test_rfp_emails_router.py`, `test_rfp_email_ingest.py`,
`test_rfp_portal_router.py`, `test_rfp_created_router.py`: 429 passed. Whole
suite: 4974 passed, 1 skipped, 0 failures. Dev backend on :5051 reloaded
the router: unauthenticated GET /rfp-processing/summary answers 401 (the
flag gate passed, auth refused), not 404.

### Frontend, 2026-09-23 (code only, uncommitted)

Files:

- `bdr_fe/lib/rfpProcessing.ts` (new): lane vocabulary (`RFP_REVIEW_LANES`,
  `isRfpReviewLane`, `RFP_PROCESSING_LANES`, `isRfpProcessingLane`,
  `rfpProcessingHref`, `rfpProcessingLaneKey`), the step order
  (`RFP_PROCESSING_STEPS`), stuck kinds, `RfpProcessingSummary`,
  `RfpProcessingItem`, the three fetchers, `isRfpProcessingRetryable`
  (email + stuck kind failed / retrying / model_wait), `rfpProcessingBadgeCount`
  (`review_total + stuck`), `RFP_PROCESSING_ROLES = RFP_EMAIL_VIEW_ROLES`.
- `bdr_fe/components/rfpEmailBits.tsx` (new): `STATUS_TONE`, `ANSWER_TONE`,
  `confidencePercent`, `personText`, `recipientsText`, `StatusBadge`,
  `FlagReasonText`, `ModelVerdict`, `KeywordChips`, `HARVEST_CHIP_TONE`,
  `HarvestChip`, moved verbatim out of the queue page and exported.
- `bdr_fe/components/RfpEmailDetailModal.tsx` (new): `DetailModal` moved
  verbatim (with `InertBody`, `conflictSentence`, `AuthCell`,
  `BlockSenderModal` and their constants) and exported as
  `RfpEmailDetailModal`. A line diff against the old page region shows only
  the rename plus the additions: `promptReview` prop, the review prompt
  state, the header "Review" button, and the sub-dialog mount.
- `bdr_fe/components/RfpReviewDecisionModal.tsx` (new):
  `RfpReviewDecisionModal` (email, per status) and
  `RfpPortalReviewDecisionModal` (NGEM `review_match`).
- `bdr_fe/app/(app)/rfp-emails/page.tsx`: imports the dialog and the bits;
  the table, tabs, paging and gate are byte-identical except
  `<DetailModal` became `<RfpEmailDetailModal` (same props, no
  `promptReview`, so no automatic prompt there; the Review button shows).
- `bdr_fe/components/RfpPortalInvitationModal.tsx`: `promptReview` and
  `canAct` (default true) props, header Review button, portal sub-dialog.
- `bdr_fe/app/(app)/rfp-processing/page.tsx` (new): the page.
- `bdr_fe/components/RfpEmailsActivity.tsx`: `processing:
  RfpProcessingSummary | null`, read in `refreshCounts` under the email
  counts' enable rule and 30 s cadence; a failed read keeps the last value.
- `bdr_fe/components/Sidebar.tsx`: `RFP_PROCESSING_NAV` right after
  `RFP_EMAILS_NAV` in `BIDDING_NAV` and in the Bidding-off carry-over list;
  badge `review_total + stuck`, red (`bg-error`) badge and dot while
  `stuck > 0`, navy otherwise.
- `bdr_fe/app/globals.css`: `.review-glow` (navy ring one surface gap off
  the navy button plus a breathing `--navy-ring` halo, same box-shadow
  technique as `.lane-glow`, no animation under reduced motion).
- `bdr_fe/locales/{en,ceb,fil,hi,sw,ur}/translation.json`: `nav.rfpProcessing`,
  `nav.rfpProcessingWaiting_one/_other`, the `rfpProcessing.*` block (after
  `rfpEmails`), `rfpEmails.review.*` (sub-dialog), `rfpEmails.detail.actions.review`,
  `rfpEmails.status.split` and `rfpPortal.status.split` ("Splitting files";
  the split status had no label). English in every catalog. The catalogs
  were rewritten through a JSON load/dump that round-trips each file byte
  for byte, so nothing else moved.

Behavior:

- Review prompt state per dialog open: `auto` (only when `promptReview` and
  the viewer may act), `open`, `closed`. The first successful detail load
  resolves `auto` inside the fetch callback (no setState in an effect): a
  human-lane status opens the sub-dialog, anything else closes it for good,
  so it fires at most once per open. Closing the sub-dialog (outside click,
  Escape, "Decide later") sets `closed` and the detail stays open; the header
  Review button sets `open`. The sub-dialog is only rendered while the loaded
  row is still in a human lane, so a row that moved (the action, or a 409)
  drops it and the detail's notice is in view. The sub-dialog also repeats
  the detail's notice and error. It is mounted before the Block sender
  dialog (email) and the confirm dialogs (portal), so those stack above it.
- Every sub-dialog control is the detail's own call through its `act`:
  review yes / no and dismiss / continue with `closeAfter`, the match section
  and footer (`RfpMatchSection` + `RfpMatchFooterActions`, same props), and
  for the portal `onResolve` ("Merge into", also through `RfpMergeSearch`) and
  the existing "new" / "ignore" confirm flows.
- The page reads the summary from the activity provider (one 30 s poll for
  the nav badge and the page, re-read by `refreshCounts` after every action)
  and polls the list itself every 30 s while visible. Lane in `?lane=`;
  `?email=` / `?invitation=` deep links open a dialog exactly like the queue
  page (row clicks open it without writing the URL, also like the queue).
  Accountant: no Action column, dialogs open read-only.

Deviations and readings:

1. Retry for `stuck.kind` failed / retrying / model_wait only (stalled rows
   and portal rows get no Retry), per section 1 and 2.3.
2. "Waiting since" shows the age and timestamp of `received_at` (the sort
   key), measured against the moment the page loaded (no clock in render).
3. The Detail cell reads the backend's additive `llm_answer` /
   `llm_confidence` for review_llm rows (backend deviation 1); absent, it
   says "The model could not decide".
4. The portal dialog gained an optional `canAct` (default true, so the NGEM
   tab is unchanged); /rfp-processing passes the viewer's review right. With
   backend deviation 2 the Accountant never gets a portal row anyway.
5. `npx next lint` no longer exists in Next 16 (it reads `lint` as a project
   directory); the project's lint script is `eslint`, run as `npx eslint`.
6. Not exercised in a browser (the dev app is behind sign-in and 2FA); tsc
   and eslint only.

Gates (run in `bdr_fe`): `npx tsc --noEmit` exit 0, no output.
`npx eslint` (whole tree): 0 errors, 2 warnings, both pre-existing and in
code this change did not write (`NotificationPrefsSection.tsx:100` and the
`Sidebar.tsx` touch-collapse effect, `react-hooks/exhaustive-deps`). No
`npm run build` was run.
