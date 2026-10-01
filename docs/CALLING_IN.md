# Calling In

Design record for the Calling In page (migration 0142, DEV ONLY until release).
Owner decisions locked 2026-09-30. Keep this file true when behavior changes.

## 1. What it is

After we send proposals, someone calls every GC we bid to and asks about our
proposal. There are two call rounds per project:

- **Before bid** (`pre_bid`, "List 1"): after our proposal went to the GC and
  before the project's actual bid date and time.
- **After bid** (`post_bid`, "List 2"): from the actual bid date and time for
  10 days. Days 1 to 7 are the target call window.

The page lists projects that still have GCs to call in each round. A GC is done
for a round only when a call with outcome **Spoke with them** is logged for it.
When every GC on a project is done for a round, the project leaves that list.

## 2. Owner decisions (2026-09-30)

1. **Actual bid date is shown on this page to everyone who can open it.** The
   confidentiality rule for `actual_bid_at` exists to hide it before the bid is
   sent; every project on this page has already been sent, so the rule no
   longer applies here. This is a deliberate, page-scoped exception. Do NOT
   change the redaction anywhere else (project API, Bids Today, Bid
   Invitations, analytics).
2. **List 2 timing:** a project lands on List 2 at the actual bid time. Days
   are Pacific calendar days since the bid day: day 0 is the rest of the
   bid's Pacific calendar day = "opens tomorrow" (calls can still be logged),
   day 1 starts at the next Pacific midnight, days 1 to 7 = "call now", days 8
   to 10 = "overdue", and it clears itself 10 days after the actual bid time
   (not 10 days after landing).
3. **Outcome is required on every call:** Spoke with them / Left voicemail /
   No answer. Only **Spoke with them** marks the GC done. Voicemail and no
   answer are logged as attempts and keep the GC open.
4. **Missing bid time or date:**
   - Date known but time unknown: the project moves to List 2 at the end of
     that Pacific day (23:59:59.999 America/Los_Angeles).
   - No actual bid date: the project stays on List 1 with a "No actual bid
     date" warning and never reaches List 2 until the date is set.
5. **Win/Loss recorded before we call:** the project stays on List 2 and the
   page clearly shows Won or Lost (project level, and per GC when the Win/Loss
   grid has a per-GC result).
6. **Notifications:** in-app plus email to Executive and Estimating Engineer
   Labor, once when a project lands on each list (a late GC, a date entered
   after the fact or a postponement is a new landing and notifies; a
   correction that re-opens a list does not; section 5). No reminders.
7. **Sidebar badge:** a red count on the Calling In icon showing how many list
   entries are still active (open). Shown to writer roles.
8. **Project page call log:** read-only side-menu item on the bidding project
   page showing every call for that project, both rounds.

**Analytics and the actual bid date.** `GET /analytics/calling-in` carries no
`bid_at` field, but two of its values are derived from `T`: a missed pre_bid
row's `window_closed_at` equals `T`, and a post_bid `hours_to_call` is measured
from `T`. Both fall under the same page-scoped exception as decision 1: every
slot is on a project whose proposal was already sent, and those values only
exist once the project is past its bid (a missed window has closed; a post_bid
slot only counts once `T` has passed). Pre_bid `hours_to_call` is measured
from `sent_at` and reveals nothing about `T`.

## 3. Rules (computed live, never stored as flags)

Membership is always computed from current data, so a postponed bid date, a
late GC, or an edited call is reflected on the next read with no migration of
state.

**Eligible GCs** for a project: `proposal_sends` rows with `status = 'sent'`,
any `sent_via` (email or external) and any origin (Send Out, Mark as
submitted, the 0140 external-submission wizard). One row per (project, GC).
`sent_at` is when that GC got our proposal. GCs we never sent to are not on
this page. A re-send does not reset anything.

**Excluded projects:** abandoned or declined, test-session projects
(`test_session_id` not null), and projects with no eligible GC.

**Effective bid time `T`:** `projects.actual_bid_at`.
- Date-only detection: treat `T` as date-only when the 0130 flag
  `rfp_created_projects.bid_time_unknown` is true for the project AND
  `actual_bid_at` is still exactly midnight America/Los_Angeles, OR when
  `actual_bid_at` is exactly 00:00:00 Pacific (nobody bids at midnight). A
  date-only `T` becomes the end of that Pacific day.
- `T` null: see decision 4.

**List 1 (pre_bid)** includes a project when `now < T` (or `T` is null) and at
least one GC with `sent_at < T` (any `sent_at` when `T` is null) has no
`spoke` call in round `pre_bid`. Only GCs sent before `T` are shown in round
`pre_bid`; a GC first sent after `T` belongs to round `post_bid` only.
A null-`T` project also leaves List 1 once a bid outcome is recorded (the
project has clearly bid) or the project is closed.

**List 2 (post_bid)** includes a project when `T` is set, `T <= now < T + 10
days`, and at least one eligible GC (sent any time) has no `spoke` call in
round `post_bid`. Bands by whole days since `T` in Pacific time: day 0
`opens_soon`, days 1 to 7 `call_now`, days 8 to 10 `overdue`.

**Late GC:** a new GC sent before `T` re-opens List 1 for that project with
only that GC outstanding (a new list entry, which notifies again).

**Logging window:** a call may be logged for a GC in a round only while that
round's window is open for that GC (pre_bid: GC eligible and `now < T` or `T`
null; post_bid: `T <= now < T + 10 days`). An already-done GC can still get
extra calls logged inside the window (history). Outside the window the API
refuses with 409.

**Implementation notes (backend, 2026-09-30):**
- Date-only detection needs no read of `rfp_created_projects`: the 0130 flag
  only ever marks a value stored at exactly midnight Pacific, so the flag case
  is a subset of the midnight rule (`effective_bid_time`).
- Day bands count Pacific calendar days (`now` date minus `T` date in
  America/Los_Angeles), so day 1 starts at the first Pacific midnight after
  `T`. `T + 10 days` is 10 Pacific calendar days at `T`'s wall time, so a
  window spanning a clock change is 239 or 241 real hours. A date-only `T`
  (23:59:59.999) therefore goes straight to day 1 (`call_now`) at midnight.
- No window is open on an excluded project (abandoned, declined, test
  session): logging a call there is a 409, and analytics skip those projects.
- For a null-`T` project, a recorded bid outcome (any result, including
  no_award) also closes the pre_bid logging window, at `bid_outcomes.recorded_at`
  (analytics count an uncalled GC there as missed at that moment).
- A `proposal_sends` row at `sent` without `sent_at` cannot be placed before
  or after `T` and is not eligible for either round (none exist on dev).
- Close reasons: `window_closed` when the bid time passed, the 10 days ran
  out, or a null-`T` project recorded its outcome; `cleared` when every GC is
  done; `left` otherwise (postponement, abandon, `T` cleared, GCs removed).
- The request that logs, edits or deletes a call only CLOSES entries (with
  their notifications dismissed); claiming and notifying stay with the poller,
  so a deleted spoke call re-opens the list on the next poller tick (claimed
  silently, section 5).
- Analytics do NOT count a missed slot whose window closed before the go-live
  marker (`call_in_meta.started_at`, section 6), so bids that closed before
  Calling In existed never read as `missed`.

## 4. Data model (migration `0142_calling_in.sql`)

- `call_in_calls`: `id`, `project_id` (fk projects, cascade), `gc_id` (fk
  general_contractors, restrict, like proposal_sends), `round` text check in
  ('pre_bid','post_bid'), `outcome` text check in
  ('spoke','voicemail','no_answer'), `note` text not null check
  (length(btrim(note)) > 0), `contacts` jsonb not null (snapshot array of
  `{gc_contact_id, name, phone, email}`, at least one), `called_at`
  timestamptz default now(), `created_by` (fk profiles, set null),
  `created_at`, `updated_at` (+ shared `set_updated_at` trigger), `edited_by`.
  Indexes on (project_id, round, gc_id) and (called_at). Deny-by-default RLS
  like every other table (service-role backend).
- `call_in_entries`: one row per time a project lands on a list. `id`,
  `project_id` (cascade), `round`, `opened_at`, `notified_at`, `closed_at`,
  `close_reason` text check in ('cleared','window_closed','left'). Partial
  unique index on (project_id, round) where closed_at is null. This is the
  multi-worker claim for notifications (2 uvicorn workers): insert-then-notify,
  so only the worker that won the insert sends.
- `call_in_meta`: a single row (`id` boolean primary key, check `id` is true,
  so at most one row), `started_at` timestamptz not null default now(),
  `created_at`. 0142 inserts the row idempotently, so `started_at` is when
  0142 was applied to that database: the go-live marker for analytics.
  Deny-by-default RLS like the other tables. A missing row reads as now.
- New GC contacts added from the call screen are ordinary `gc_contacts` rows
  (name required, phone and email optional) on that GC, so they show up
  everywhere else.

## 5. Poller and notifications

`app/services/calling_in.py` holds the pure membership computation (the tested
seam, no I/O) plus a poller wired in `main.py` lifespan behind
`CALL_IN_ENABLED` (default true; also requires bidding + supabase). Every 60s:

1. Compute open entries (project, round).
2. Close FIRST: for each open `call_in_entries` row whose entry is no longer
   open, set `closed_at` + reason (`cleared` when every GC is done,
   `window_closed` when the bid time passed or the 10 days ran out, `left`
   otherwise, for example a postponement or abandon) and dismiss that entry's
   notifications. Closing before claiming means a List 1 to List 2 move (or
   back) in the same tick sees the fresh `closed_at` as the new entry's
   trigger.
3. Claim: for each open entry with no open `call_in_entries` row, insert the
   row. If the insert wins and the notify rules below pass, notify Executive +
   Estimating Engineer Labor (`notify_role`, types `call_in_pre_bid` /
   `call_in_post_bid`, email mirror through the existing notification email
   path and prefs) and stamp `notified_at`. Otherwise the entry is claimed
   silently (`notified_at` stays null); it still counts in the badge and
   shows on the page.

**Trigger** (the moment an entry became due). "Other-round close" is the
`closed_at` of this project's most recent `call_in_entries` row (by
`opened_at`) in the OTHER round, when that row is closed.
- List 1 (`pre_bid`): max(newest pre_bid-eligible `sent_at`, other-round
  close).
- List 2 (`post_bid`): max(`T`, newest eligible `sent_at`, other-round close).

So a proposal recorded as sent days after `T` (Mark as submitted, the 0140
wizard: `sent_at` = now), a null-date project whose date is entered after the
fact (List 1 closes now, List 2 opens now), and a postponement that moves a
project back onto List 1 (List 2 closes now) all notify when they happen,
instead of counting from an old `T`.

**Notify rules** for a newly claimed entry:
1. Burst guard: the trigger is within the last 24 hours. This still protects
   the release: with no prior entries, everything already due is claimed
   silently. After a long poller outage, a project that moved between lists
   during the outage notifies once when the poller catches up (its
   other-round close is the catch-up tick).
2. No re-notify: skip when an earlier entry for the same (project, round)
   already has `notified_at` set and its `opened_at` >= the current trigger.
   A correction that re-opens a list (a Spoke call edited to voicemail, or
   the call that cleared the list deleted) is therefore claimed silently. A
   genuinely new trigger (a late GC sent later, or a round transition) is
   newer than every earlier entry and still notifies.

Two-worker detail: another worker may close the other round a few
milliseconds after this tick began. The claim is stamped at
max(tick time, other-round close) and the trigger's other-round close is
capped at that moment, so the burst guard never sees a trigger in the future
and the new entry's `opened_at` is never before its trigger.

Logging the call that clears a project also closes the entry and dismisses the
notifications inside the request, so the badge and bell update right away (the
poller is the backstop). Tests pin `CALL_IN_ENABLED=false` in conftest.

## 6. API contract (binding for backend and frontend)

All routes: reads `require_internal` (writers + read-only Accountant), writes
`require_writer`. Handlers are plain `def` (sync Supabase SDK). Times are ISO
8601 UTC; the FE renders Pacific via `lib/format`.

### `GET /calling-in/summary`
`{ "open_count": int, "pre_bid": int, "post_bid": int }` for the sidebar badge.

### `GET /calling-in`
```
{
  "now": iso,
  "pre_bid":  [Entry],
  "post_bid": [Entry]
}
Entry = {
  "project_id": uuid, "project_number": str|null, "project_name": str,
  "round": "pre_bid"|"post_bid",
  "bid_at": iso|null,            // effective T (end of day when date-only)
  "bid_at_date_only": bool,
  "bid_at_missing": bool,
  "window_closes_at": iso|null,  // pre_bid: T; post_bid: T + 10 days
  "band": "open"|"no_bid_date"|"opens_soon"|"call_now"|"overdue",
  "days_since_bid": int|null,    // post_bid only
  "outcome": "won"|"lost"|null,  // project-level bid outcome if recorded
  "gcs_total": int, "gcs_done": int,
  "entered_at": iso|null,        // open call_in_entries.opened_at
  "on_list": bool                // additive: currently on this round's list
}
```
`on_list` is an additive field (always true on `GET /calling-in`); the
project detail uses it for a round the project is not on. For such a round
the band still comes from the enum: post_bid before `T` is `opens_soon` with
`days_since_bid` null, post_bid with no `T` is `no_bid_date`, and a post_bid
round past its 10 days keeps `overdue` with the real day count.
Sort: pre_bid by `bid_at` ascending (missing dates last); post_bid by `bid_at`
ascending (oldest, most urgent first).

### `GET /calling-in/projects/{project_id}?round=pre_bid|post_bid`
```
{
  "entry": Entry,               // same shape; 404 if project has no eligible GCs
  "gcs": [{
    "gc_id": uuid, "gc_name": str,
    "proposal_send_id": uuid, "sent_at": iso, "sent_via": "email"|"external",
    "amounts": { "total": number|null,
                 "sections": [{ "key": str, "label": str, "amount": number }] },
    "gc_outcome": "won"|"lost"|null,  // bid_gc_outcomes.gc_award_result
    "done": bool, "done_at": iso|null,
    "window_open": bool,
    "contacts": [{ "id": uuid, "name": str, "phone": str|null,
                   "email": str|null, "is_project_contact": bool }],
    "calls": [Call]              // this round only, newest first
  }]
}
Call = {
  "id": uuid, "project_id": uuid, "gc_id": uuid, "round": str,
  "outcome": "spoke"|"voicemail"|"no_answer", "note": str,
  "contacts": [{ "gc_contact_id": uuid|null, "name": str,
                 "phone": str|null, "email": str|null }],
  "called_at": iso, "created_by": { "id": uuid|null, "name": str|null },
  "updated_at": iso, "can_edit": bool, "can_delete": bool
}
```
`amounts` come from the same per-GC source the Win/Loss grid and proposal use
(what that GC actually holds). `is_project_contact` = selected in
`project_gc_contacts` (0110). This works for any round value even when the
project is not currently on that list (the call log and history use it).
`round` is optional: without it the detail opens on the list the project is
on, else `post_bid` once `T` has passed, else `pre_bid`. `gcs` are sorted by
name; each GC's `contacts` list project contacts first, then by name.
`gc_name` is the GC's current name (the send-time snapshot as a fallback).

### `POST /calling-in/projects/{project_id}/calls`
Body: `{ "gc_id", "round", "outcome", "note", "contact_ids": [uuid],
"new_contacts": [{ "name", "phone"?, "email"? }] }`. At least one contact
(existing or new) required; note non-blank; outcome required. `contact_ids`
must belong to that GC (422 otherwise). New contacts are created on that GC
first. 409 when the round window is closed for that GC or the GC is not
eligible. Returns `Call` with status 201. Audit `call_in.log`. Limits: note
up to 4000 characters, new contact name up to 200, phone up to 50, email
must be a valid address (blank phone or email means none).

### `PATCH /calling-in/calls/{call_id}`
Body: any of `{ "outcome", "note", "contact_ids", "new_contacts" }`. Author
only (403 for anyone else). `contact_ids`, when sent, replaces the call's
contacts; `new_contacts` are created on the GC and added; the result must
keep at least one contact (422). No window check on edits. Returns `Call`.
Audit `call_in.edit`.

### `DELETE /calling-in/calls/{call_id}`
Executive and IT Admin only. 204. Audit `call_in.delete`.

### `GET /projects/{project_id}/call-log`
```
{
  "on_list": "pre_bid"|"post_bid"|null,
  "bid_at": iso|null, "bid_at_date_only": bool,
  "rounds": {
    "pre_bid":  { "gcs": [{ "gc_id", "gc_name", "done": bool, "calls": [Call] }] },
    "post_bid": { "gcs": [ ...same... ] }
  }
}
```
`bid_at` here follows the normal redaction (null for roles outside
ACTUAL_BID_VIEWER_ROLES) because this lives on the project page, not on
Calling In.

### `GET /analytics/calling-in?range=&start=&end=`
Same `RangeParam` (`alias="range"`) + custom start/end as the other windowed
analytics routes. The custom bounds are accepted as `start`/`end` or as
`date_from`/`date_to` (what the other analytics tabs send through
`lib/format`); `start`/`end` win when both are present. A custom range
without both bounds is a 400. A "slot" is (project, GC, round). Slot status: `called` (has
a spoke call), `missed` (window closed, no spoke), `open`. Time to call =
first spoke `called_at` minus `sent_at` (pre_bid) or minus `T` (post_bid).

Range anchoring: each slot sits on the range at the moment its call window
OPENED: a pre_bid slot at that GC's `sent_at`, a post_bid slot at `T`. Slots
whose window has not opened yet (a post_bid slot before `T`) are left out.
So the preset ranges, which end at now, include the pre_bid slots of a bid
tomorrow (sent this month) but not its post_bid slots.

Go-live marker: `call_in_meta.started_at` (section 4). A `missed` slot counts
only when its window closed at or after `started_at`; `called` and `open`
slots always count (calls logged always count). If the row is missing,
`started_at` is treated as now (nothing historic counts as missed) rather
than erroring.
```
{
  "rounds": {
    "pre_bid":  { "slots": int, "called": int, "missed": int, "open": int,
                  "call_rate": number|null,        // called / (called + missed)
                  "median_hours_to_call": number|null,
                  "avg_hours_to_call": number|null },
    "post_bid": { ...same... }
  },
  "by_gc": [{ "gc_id", "gc_name", "called": int, "missed": int,
              "median_hours_to_call": number|null }],
  "calls": [{ "project_id", "project_number", "project_name", "gc_id",
              "gc_name", "round", "called_at", "called_by_name",
              "contact_names": [str], "hours_to_call": number,
              "attempts_before": int }],
  "missed": [{ "project_id", "project_number", "project_name", "gc_id",
               "gc_name", "round", "window_closed_at" }]
}
```
No `bid_at` field in the analytics payload; the values derived from `T`
(`window_closed_at`, post_bid hours to call) are covered in section 2.

## 7. Frontend

- Route `/calling-in`, sidebar entry "Calling In" in BIDDING_NAV with a phone
  icon (add to `components/ui/icons.tsx`), red count bubble from
  `/calling-in/summary` for writer roles, path added to `subAppForPath`.
- Two tabs, "Before bid" and "After bid", each with its count. Rows show
  project, actual bid date and time with time left (List 1) or day N of 10
  with the band chip (List 2), GCs done x of y, Won/Lost badge, the "No actual
  bid date" warning, and a "time unknown, moves at end of day" note.
- Clicking a project opens the detail: each GC with the pricing we sent (total,
  sections on expand), done state, attempt count, per-GC Won/Lost. Clicking a
  GC shows contacts (phone as `tel:`, email as `mailto:`, "Project contact"
  badge) and call history, plus **Log call** (writers only).
- Log call modal: contacts multi-select (at least one) with inline "Add new
  contact" (name required, phone, email), outcome required with the helper
  "Only Spoke with them marks this GC done", note required.
- Project page: "Call log" side-menu item (read-only modal, both rounds, per
  GC done state and calls, "Open in Calling In" when `on_list` is set).
- Analytics: new "Calling In" tab in `AnalyticsTabs` (`/analytics/calling-in`)
  with per-round tiles, by-GC table, calls table, missed table, InfoHint
  captions via the metricInfo registry.
- No em dashes in any UI text.

## 8. Release

Apply 0142 to staging, then prod WITH APPROVAL; reload PostgREST; deploy BE
and FE together after it.

- **0142 depends on 0131.** The Calling In reads select
  `projects.test_session_id` (0131), so 0131 must be applied to the target
  database first (it is, when 0119 to 0141 ship first, which the "apply after
  0141" order implies).
- **0142 includes the go-live row.** It creates `call_in_meta` and inserts
  its single row; that insert time (`started_at`) is go-live on that
  database, and analytics only count missed slots whose window closed at or
  after it. Apply 0142 at release time, not early, so go-live matches when
  the feature actually ships.
- **Release check (before release, with approval, read-only on prod):**
  count prod projects with a sent proposal, no actual bid date and no bid
  outcome. Each one would sit on List 1 permanently ("No actual bid date")
  until someone enters the date or records the outcome, so review the count
  (and the list) with the owner first:

  ```sql
  select count(*)
  from projects p
  where p.actual_bid_at is null
    and p.abandoned_at is null
    and p.current_stage not in ('declined', 'pm_only', 'cp_only')
    and exists (select 1 from proposal_sends ps
                where ps.project_id = p.id and ps.status = 'sent')
    and not exists (select 1 from bid_outcomes o where o.project_id = p.id);
  ```

  (No `test_session_id` filter so it runs before 0131; test sessions only
  exist after 0131.)
- DEV (bpidntbyvoooqvaispup): 0142 applied 2026-09-30; its `call_in_meta`
  amendment was applied the same day as the delta `0142b_calling_in_meta`
  (dev go-live `started_at` 2026-09-30 19:20:21 UTC). Staging and prod get
  the whole amended 0142 in one go.

`CALL_IN_ENABLED` defaults true, so no Railway var is needed to turn it on.
The 24-hour burst guard (section 5) means projects already on a list at
release appear on the page and in the badge without a flood of notices.
