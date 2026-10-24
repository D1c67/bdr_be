# Revise proposal (revised proposals after submission)

Status: built 2026-10-23 on `urgent-updates`, migration 0178 applied to dev
(BDR) only. Not committed, not deployed.

## 1. The user story

A bid was submitted (proposals sent to GCs). The GC then pushes the bid date
out and the team wants to revise its numbers and send a "Revised Proposal".
The project's right panel gets a **Revise proposal** item (`?box=revise_proposal`).

Owner decisions (final):

1. The entry point is the project side menu, next to Mark submitted / Scope
   lines / GCs.
2. Available only while the bid is Submitted with no win/loss recorded: the
   send_out lane head is `submitted` (or parked at `verify` with
   `projects.reverify_return_stage = 'submitted'`). Hidden for abandoned and
   failed-to-bid projects.
3. Roles: the roles that send proposals today (`require_writer`, the Send Out
   guard). Estimators and the Accountant cannot write; the Accountant opens
   the box read only (history and the open revision's state).
4. During a revision the team may change the cost side (quotes / selection,
   labor, markup), the per-GC numbers, the scope lines, and the extra files on
   the email.
5. Any cost-side change goes through the Executive's re-Verify before anything
   can be sent (the existing bounce). GC-specific numbers and scope lines do
   not need re-verify (same as at Send Out).
6. Every GC that holds a sent proposal is preselected; unchecked GCs keep
   their proposal. GCs added during the revision get a normal initial
   proposal at the revised numbers.
7. The email subject and body are editable. Defaults: subject
   `[G3 Electrical] Revised Proposal: {name} ({number})`, body a revised
   version of the Send Out cover note. The document is titled "Revised Bid
   Proposal", its date line reads `MM/DD/YYYY (Revision N)` and its filename
   ends in ` Rev N`.
8. "Mark revision submitted" records a revised proposal sent outside the app.
9. Rev 1, Rev 2, ...: one open revision per project at a time.
10. Start takes a required "What changed" and optional new bid date(s).

## 2. Data model (migration 0178)

`proposal_sends` stays ONE row per (project, GC): "the bid this GC holds".
It is never widened and never deleted (a delete cascades
`proposal_send_events`). Its row stays at `sent` for the whole revision, so
"was this GC bid" never flickers for the dozen consumers that read it
(outcome, analytics, auto send, PM, external submission, merge, calling in,
reports, files, pricing, projects, ...). New columns:

| Column | Meaning |
|---|---|
| `revision_number int not null default 0` | 0 = the original; N = the GC holds Revision N |
| `subject text` | The exact subject the current document last went out under (null = the derived Send Out subject, every pre-0178 row) |
| `revised_at`, `revised_by` | When / by whom the latest revised proposal reached the GC (null = never revised) |

The live row's **submission record never moves**: `sent_at`, `sent_by`,
`sent_via`, `email_log_id` and `gc_email` stay the original submission, the
same rule re-sends follow. Only "which document the GC holds" moves:
`file_id`, `draft_id`, `lines_hash`, `gc_name`, the five amounts, `subject`,
`revision_number` (plus `origin = 'send_out'`, `auto_send_run_id = null`,
both snapshotted first).

`proposal_send_events.kind` now allows `revision`, and the table gained
`proposal_revision_id` (set on revision events, and on the initial events of
GCs added and bid while a revision was open).

`proposal_revisions`: one row per revision. `revision_number` (unique per
project), `status` open | completed | cancelled, `reason`, the previous and
new bid dates, started / completed / cancelled by and at, `completion`
auto | manual. Partial unique index `proposal_revisions_one_open`.

`proposal_revision_sends`: one row per (revision, GC that held a sent
proposal at start). `included` (checked), its own lifecycle
`pending -> generated -> sending -> sent | failed`, `draft_id`, `lines_hash`,
`file_id`, the stamped amounts, the claim fields (`gc_email`, `subject`,
`body`, `cc_recipients`, `claimed_at`, `claimed_by`), `sent_at/by/via`,
`email_log_id`, `send_event_id`, the `prior_*` snapshot of the live values it
replaced (file, draft, lines hash, delivery time and by, sent via, email log,
recipients, subject, the five amounts, revision number, origin, auto send
run), and `start_overrides` (the GC's project_gcs per-GC overrides when the
revision started). `proposal_revisions.start_gc_ids` lists the GCs on the
project at the start.

Three SQL functions, all of which lock the revision row FIRST (then the
revision send rows), so they serialize on it and never deadlock:

| Function | What |
|---|---|
| `claim_proposal_revision_send` | The revision must be open; the row checked, at `generated` / `failed`, not delivered-but-not-recorded; no re-send of this GC's live proposal in flight. Writes the claim (`sending`, exact To string, subject, body, CC, `claimed_at`, `claimed_by`) |
| `apply_proposal_revision_send` | The swap: revision open, live row `sent`; appends the `revision` event, copies the prior values, moves the held document, stamps `revised_at` / `revised_by` |
| `finish_proposal_revision(mode)` | `done` / `cancel` / `auto`. Refused while any row is `sending`. Cancel refused once anything may have reached a GC (a sent row, an outcome-unknown or delivered-but-not-recorded row, an initial proposal tagged with the revision). Done refused when nothing reached anyone |

Null return = no longer claimable (a racer won). Refusals are raised as
`proposal_revision:<code>` and mapped to messages by `proposal_revisions._rpc`.

## 3. Endpoints

All under `/projects/{project_id}/proposal-revisions`, router
`app/routers/proposal_revisions.py`, service `app/services/proposal_revisions.py`.
Reads: `require_internal`. Writes: `require_writer`. Every write names the
open revision's id; a stale id is a 409.

| Method + path | What |
|---|---|
| `GET ""` | `{open, history, can_start, start_blocked_reason, next_revision_number}`. `open` carries the per-GC rows (document, status, stamps, `stale_reason`, contacts, what the GC holds now), `added_gcs`, `head`, `waiting_on_verify`, `pricing_committed`, `can_cancel`, the default subject/body. `history` lists past revisions with each GC's revised and prior documents and the GCs bid an initial proposal during it. The actual bid date fields are nulled for roles that may not see it |
| `POST ""` | Start. `{reason, internal_bid_at?, actual_bid_at?}`. The dates go through `PATCH /projects/{id}` (role rule, outbound email budget, the bid date change email), only after the start checks pass |
| `PUT /{rev}/gcs` | `{gc_ids}`: the checked GCs. A GC whose revised document is sent or sending stays checked |
| `POST /{rev}/generate` | `{gc_ids?}`: render the revised documents |
| `POST /{rev}/send` | `ProposalDispatchIn` + `subject` + `force`; `proposal_ids` / `contacts` / `cc` keys are revision send ids. Returns `{results, overview}` |
| `POST /{rev}/mark-submitted` | `{proposal_ids}` (revision send ids). Returns `{results, overview}` |
| `POST /{rev}/complete` | Done |
| `POST /{rev}/cancel` | Cancel |

The project wire (`GET /projects`, `GET /projects/{id}`) carries
`proposal_revision_open` and `proposal_revision_number` for the badge.

## 4. Lifecycle

**Start** refuses: not submitted (head not `submitted`, and not `verify` on
its way back to `submitted`), head `bid_outcome` or a `bid_outcomes` row,
another revision open, abandoned, failed to bid, declined, any send in
flight (a `sending` proposal_sends / proposal_send_events / revision row),
no GC on the project holding a sent proposal. Creates Rev N plus one row per
sent GC, all checked. Audit `proposal.revision_start`.

Starting while the lane is bounced to Verify from Submitted is allowed on
purpose: the bid is still submitted (an edit made just before the decision to
revise must not hide the button), and nothing can be generated until the
re-commit anyway.

**Generate** follows the Send Out rules: head must be `submitted` (at
`verify` it refuses with "waiting on the Executive's re-verify"), committed
pricing, latest draft approved with lines, no send in flight, no GC pending
an approval, all-or-nothing render with `validate_output` (which now also
demands the revision markers), compare-and-set on the row it replaces. Only
checked, not-yet-sent rows are targets. The live rows are never touched.

**Send** is crash-safe like `send_proposals`: claim in SQL -> the
pending-approval re-check -> for EACH GC, right before its email, the head
must still be `submitted` and the figures are re-read -> `assert_send_isolation`
on the exact bytes (right
GC, `Proposal NNNN - GC Rev N.docx`, no other GC's name, stamped amounts
present and equal to today's per-GC figures, recipients are live contacts and
equal the claimed string, latest approved draft and lines hash, revision
markers) -> PDF + PDF re-scan -> Graph -> the swap. After Graph accepted the
email: a swap REFUSED by the function (a `proposal_revision:<code>`) makes the
row `failed` with "email delivered but not recorded: ..." (terminal: never
re-sent, not markable, Cancel refused, Done closes it, the UI says so
plainly); a transport error leaves it `sending` for the reclaim. A row stuck
at `sending` for 10 minutes is reclaimed (`reclaim_stale`) on EVERY read of
the box (GET) and before every revision action, by `email_log` proof on the
stored subject AND the exact To string AND `created_at >= claimed_at`: proof
finishes the swap (or marks it delivered but not recorded), no proof makes it
`failed` with "outcome unknown", which needs `force` to retry. So a crashed
send never wedges the project.

**Mark revision submitted** runs `assert_mark_ready` (draft, lines, amounts
freshness) and the swap with `via = 'external'` (no email, null recipients,
the default revised subject stored for a later re-send).

**Complete** happens on its own when every checked GC is sent or marked and
no GC added during the revision is still waiting on its initial proposal (a
proposal_sends row at generated / failed / sending touched since the start).
A GC linked after the start with no sent proposal (even before anything was
generated for it) also holds it open, and a checked GC removed from the
project is ignored. A revision with nothing checked stays open. **Done**
closes it explicitly; **Cancel** only while nothing may have reached a GC
(both enforced in `finish_proposal_revision`, see section 2). Cost-side edits
already made stay in place: they are not rolled back (the UI warns). On
complete and cancel, revised documents that never went out are deleted,
except a sending one or one whose outcome is unknown or that was delivered.

**Per-GC figures follow the held document.** A GC whose revised document does
not go out (unchecked, or the revision closes without it) gets its
`start_overrides` back on project_gcs (audit
`proposal.revision_amounts_restored`), so Win/Loss, analytics and a later
revision never read figures that GC was not bid.

**Start ordering.** The revision is created first; the optional new bid dates
are then applied through the project PATCH. A refused start never moves a
date or sends the bid date email. A refused date change leaves the revision
open, clears its recorded new dates and returns `bid_date_error`.

**Outcome**: `record_outcome` refuses while a revision is open ("Finish or
cancel the revision"). **Auto send** never fires: a revision only exists once
the bid went out, and `gone_out_rule` is true at both heads a revision lives
at. **External submission undo** is refused while a revision is open or once
a revised proposal went out after the submission (`undo_blocked_reason:
revised`): the undo would restore proposal_sends rows the revision replaced.

**Re-send** is refused for a GC checked in the open revision whose revised
document is not recorded as sent (it could land after the revised one), and
a revision claim is refused while a re-send of that GC is in flight.
**Re-send** (`resend_proposals`) after a revision re-sends the CURRENT
document (the revised one) under `proposal_sends.subject`; the default body
for a row with `revision_number > 0` is the revised body; the resend
isolation check expects the `Rev N` filename and the markers; the resend
reclaim accepts the stored subject as proof as well.

No stage_events for any of this (analytics derives time-in-stage from
consecutive stage_events). The re-verify bounce itself still writes its
existing submitted -> verify -> submitted events, as it did before 0178.
Audit actions, labelled in `analytics_metrics`: `proposal.revision_start`,
`proposal.revision_gcs`, `proposal.revision_generate`,
`proposal.revision_generate_docs`, `proposal.revision_send`,
`proposal.revision_send_failed`, `proposal.revision_mark_submitted`,
`proposal.revision_complete`, `proposal.revision_cancel` (plus the
unlabelled bookkeeping rows `proposal.revision_send_unrecorded` and
`proposal.revision_amounts_restored`). The notification log classifies each
revision email from its `revision` event's `email_log_id` ("Revised proposal
sent to the GC"); the live row's `email_log_id` keeps labelling the original.
A proposal document that went out (any sent transmission, or a revision's
prior or sent document) can no longer be deleted from Files.

## 5. Lock matrix

| Edit | No revision, bid submitted | Revision open, head `submitted` | Revision open, head `verify` (cost change) |
|---|---|---|---|
| Scope lines save / approve | 409 (any sent row) | Open (only a send in flight refuses). A save on a draft a sent document was built from creates a NEW draft, so the record of what each GC holds is never rewritten and re-sends keep validating | Open |
| GC amounts, GC in the revision (checked, revised doc not sent) | 409 (sent) | Writer edits directly, no 0118 approval | Writer edits directly |
| GC amounts, GC unchecked or already sent in this revision | 409 | 409 | 409 |
| GC amounts, GC added (no sent row) | Executive approval (0118) for non-executives | Writer edits directly | Writer edits directly |
| Quote selection / approval / tax / override / delete / quotes-confirmed | 0138 late-quote lock, no bounce | Behaves like a pre-Verify edit: allowed and bounces to Verify | Same |
| New quotes | Tagged `received_after_submission` (record only) | Ordinary candidates (not tagged late) | Same |
| A quote tagged late before the revision | Record only | Selecting it as the winner clears the tag: it now prices the bid, so analytics count it | Same |
| Labor, markup | Bounce to Verify (unchanged) | Bounce to Verify | Already at Verify |
| Generate / send / mark (revision) | n/a | Allowed | Refused (waiting on re-verify) |
| Send / mark (initial, added GC) | 0104/0118 rules | Allowed | Refused (waiting on re-verify) |
| Win/loss outcome | Allowed | Refused | Refused (head not submitted) |

The window switch is `workflow.in_post_submission_window`, which answers False
while a revision is open; every 0138 lock keys off it. Everything locks again
the moment the revision is completed or cancelled.

### Added GCs and the 0118 approval (decision)

A GC added during an open revision is priced by the writer directly
(`set_gc_amounts` skips the "needs executive approval" refusal while a
revision is open) and generated / sent through the normal Send Out path
(`generation_targets`: a GC with no sent row gets an initial document, normal
subject, normal wording). Why: the revision is itself a deliberate re-pricing
of the whole bid, and any cost-side change in it already goes through the
Executive's re-Verify; the per-GC figures of every other GC in the revision
are writer-editable too, so routing the added GC alone through approval would
be inconsistent. A GC already `pending` stays locked (an Executive must still
decide an open request), and the side menu's "Send proposal" approval flow
still works for anyone who prefers it.

## 6. Deliberately not built

- No rollback of pricing on cancel (owner decision).
- Revised documents are not regenerated automatically after a re-commit
  (`regenerate_after_commit` covers proposal_sends only). The overview flags a
  stale document (`stale_reason`: waiting_on_verify, lines_changed,
  amounts_changed, gc_removed) and the send fails closed on it.
- No stage_events, no new notifications beyond the existing failure bell and
  the re-verify ping (whose text now mentions the open revision).
- No per-revision email body history beyond the row's stored body.
- No "Revising" badge on the dashboard list (the project wire carries the
  flag; only the project page shows it).

## 6b. proposal_sends consumers after a revision

What each reader gets once a GC holds a revised document (sent_at, sent_by,
sent_via, email_log_id, gc_email = the original submission; file_id,
draft_id, lines_hash, amounts, subject, revision_number = the current
document):

| Consumer | Reads | Effect |
|---|---|---|
| outcome (`_sent_gcs`), pm (winning amount), analytics_metrics (bid amounts) | status, amounts | The revised figures: what we bid now |
| labor_engineer_report | sent_at | Original submission time (unchanged) |
| bid_invitations | sent_at vs needs_by | Original (on time stays on time) |
| calling_in | sent_at, sent_via, amounts | Windows from the original send; revised amounts |
| notification_log | email_log_id + revision events | Original labelled as the proposal, revision emails labelled as revisions |
| external_submission | sent_via, sent_at, amounts; undo blocked once revised | Original record; undo refused (`revised`) |
| rfp_create (`gc_email` ilike), rfp_testing, rfp_email_ingest, auto_send, auto_send_hooks, files, pricing, projects | status / gc_email / sent_at | Unchanged meaning |
| proposals (`_proposal_rows`), project_gc_rows | `*` / sent_at | Original sent_at; `revision_number`, `revised_at`, `subject` available |
| resend_proposals | file_id, draft_id, subject, revision_number, amounts | Re-sends the current (revised) document under its subject |
| project_merge, gc_aliases | table / gc_id references | proposal_revision_sends added to the GC reference list |

## 7. Tests

`tests/test_proposal_revisions.py` (62 tests, plus the review follow-ups:
GET reclaim, delivered-but-not-recorded, Done/Cancel while sending, the
re-send block, per-GC figures restored, per-GC head re-check, router roles,
start ordering): start guards, lock opening and
closing (lines, GC amounts, quote selection), the bounce and the generate
refusal until re-commit, checked-GC targeting with real rendered documents,
the live row swap with the prior snapshot and the revision event, unchecked
GCs untouched, added GC initial proposal (normal subject, tagged event),
custom subject stored and used by the reclaim, mark revision submitted,
auto-complete (including waiting for an added GC), Done and Cancel guards,
outcome refused, auto send silent, re-send of the revised document, isolation
checks on revised documents, overview and history, the external submission
undo guard. The three SQL functions were exercised on dev in rolled-back
transactions (claim blocked by a re-send in flight, finish refused while
sending, cancel refused once sent, claim refused on a closed revision, the
swap leaving the submission record untouched); the endpoints and the real
PostgREST error mapping were smoke-tested live on dev (reads, start, cancel;
no email).
