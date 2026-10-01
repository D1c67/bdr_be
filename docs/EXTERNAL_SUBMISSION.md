# Mark submitted (external submissions)

Migration `0140_external_submissions.sql`. Code: `app/services/external_submission.py`,
`app/routers/external_submission.py`, `bdr_fe/components/MarkSubmittedModal.tsx`,
`bdr_fe/components/MarkSubmittedWizard.tsx`, `bdr_fe/lib/externalSubmission.ts`.
Tests: `tests/test_external_submission.py`.

## 1. What it is

Sometimes G3 sends a proposal outside the app while the project already exists in
BDR. "Mark submitted" (project side menu, directly under Notifications) records the
numbers that went out and moves the project to Submitted from any stage past
Go/No-Go, without walking the pipeline.

The Send Out step's own "Mark as submitted" (`proposal_send.mark_submitted`) is a
different path and is unchanged: it needs generated, approved proposal documents.
This one does not.

## 2. Who can do what

| Role | Access |
| --- | --- |
| Executive, Estimating Admin, IT Admin (approvers) | Enter the wizard directly. Grant, deny or revoke other people's requests. Undo a submission. |
| Estimating Engineer (Materials or Labor) (requesters) | Must request permission first. |
| Accountant, Estimator | No access. The side-menu item is not shown and every route refuses. |

A grant is for one user on one project. It lasts 48 hours from the moment it was
granted, works once (the submit consumes it), and an approver can revoke it while
unused. Expiry is enforced on the server on every wizard read that returns data and,
atomically, inside the database function that applies the submission.

Request lifecycle (`external_submission_requests.status`):
`pending` then `approved` (grant) or `denied`; a requester can `cancel` a pending one;
an approver can `revoke` an unused grant; the submit marks it `used`; a grant that
passes its `expires_at` unused is written `expired` lazily (on the next read or new
request). One open request (pending, or approved and unused) per user per project is
enforced by a partial unique index. Nobody decides their own request (a requester
promoted to an approver role cancels it instead), and a grant is refused once the
project can no longer be marked. When a submission is applied, every other open
request on the project is closed in the same transaction (pending ones `cancelled`,
unused grants `expired`) and their bell rows dismissed, so none can be granted later
or come back to life after an undo.

Notifications (in-app bell plus the usual branded email mirror, both deep-linking to
`/projects/{id}?box=mark_submitted`):

- `external_submission.requested` to every active approver; carries
  `metadata.request_id`. The first approver to act decides it and the others' rows are
  dismissed (and they see the request gone).
- `external_submission.granted` / `.denied` (with the reason) / `.revoked` to the
  requester.
- `external_submission.undone` to the original submitter, in-app only.
- The standard `submitted` notice to both engineer focuses and the Executive, as
  "Done sending" sends.

The side-menu item shows a count badge of pending requests to approvers
(`GET .../external-submission/pending-count`, polled like the notes badge). The count
is 0 while the project cannot be marked (the modal then shows the reason, not the
queue).

## 3. When it is available

Past Go/No-Go (the intake lane is on To Estimator or complete), and not declined,
not abandoned, not a PM-only / CP-only project, not already submitted (send_out head
at Submitted or Win/Loss, or parked at Verify by a post-submission re-verify bounce),
with no recorded Win/Loss outcome and no live external submission. When unavailable
the modal shows the reason; when the project was submitted through this feature it
shows the read-only summary instead.

## 4. The wizard

1. Materials and labor: one section per pricing section (Materials, Gear and Power
   Distribution Equipment, Underground, Low Voltage; `material_categories.pricing_section`)
   plus Labor. Categories are picked from the active material categories of that
   section (no free text), each with an amount and an optional note. Empty sections
   are $0. Prefilled from the project's RFQ categories at their selected quote.
2. Markup: one percent / dollar pair per section on the bid plus labor, exactly like
   the Markup step (type either, the other is worked out). Prefilled from the
   project's markups.
3. GCs and pricing: each project GC is included (submitted to) or not (no bid). Each
   included GC's price defaults to cost plus markup and can be overridden per section
   with the same editor GC Pricing uses; an override is that GC's own markup and never
   moves the cost basis. GCs can be added here (existing company or a new one); they
   are saved to the project's GC list. A GC whose proposal already went out through
   Send Out is shown as "Already sent" and left untouched. At least one GC must be
   included.
4. Preview, then "Mark submitted" behind an "Are you sure?" confirm.

The draft is kept while the modal is open and mirrored to `localStorage`
(`bdr.markSubmitted.draft.<projectId>.<userId>`, wrapped in try/catch) so closing the
modal never loses work and a shared browser never hands one person's draft to
another. It is cleared on a successful submit. Money inputs accept what the server
accepts: no sign, no exponent, at most two decimals (three for a markup percent),
below $1,000,000,000,000; the prefill is rounded to match, and a markup percent is
seeded only when it reproduces the project's markup amount exactly.

## 5. Data model

- `external_submission_requests`: see section 2.
- `external_submissions`: one per submission. Section costs, labor, the ten markup
  columns (pct and amount per section), totals, `submitted_by`,
  `approval_request_id` (null for approvers), `status` (`active` / `undone`),
  `prior_state` (the snapshot, section 7), `stage_event_ids`, and the undo fields.
  At most one `active` row per project.
- `external_submission_lines`: one real row per material category
  (`material_category_id`, `pricing_section`, snapshot `category_name`, `amount`,
  `note`). This is the table for data studies.
- `external_submission_gcs`: one row per project GC: `included`, `already_sent`,
  the five resolved figures, total, the project default total, and links to the
  `proposal_sends` / `proposal_send_events` rows it wrote.
- `proposal_sends` gains `origin` (`send_out` default, or `external_submission`) and
  `external_submission_id`, so these rows can be reported on separately.

All four new tables have RLS enabled and forced with no policies (deny by default;
the service-role backend bypasses it), like every other table since 0055. The two
functions are `security invoker`, pinned `search_path`, execute revoked from
`public`, `anon`, `authenticated` and granted to `service_role`.

## 6. How Win/Loss, analytics and the reports see it

The submission writes the same canonical rows the normal pipeline writes, so every
reader works unchanged:

| Reader | Reads | Written as |
| --- | --- | --- |
| Win/Loss grid, bid outcome, PM contract value | `proposal_sends` status `sent`, sum of the five stamped figures | one row per included GC: `status='sent'`, `sent_via='external'`, `origin='external_submission'`, `sent_at`/`sent_by`, stamped figures |
| GC spread analytics, Estimator vs Bid report | same `proposal_sends` rows | same |
| Bid date / cohorts (analytics `submitted_at`) | first `stage_events.to_stage='submitted'` | a `send_out` event from the lane's head to `submitted` (plus an unlock event when the lane was locked) |
| Bid price (pricing summary, analytics pricing) | the committed `verifications` snapshot | committed snapshot of the ten figures; breakout sections with no lines are NULL (not on the bid) |
| Labor Numbers, Markup | `labor_reviews`, `markups` | labor amount and the pct/amount pairs |
| Per-GC prices (GC list, amounts overview) | `project_gcs.proposal_*_amount` | each included GC's own overrides (null = project price) |
| Stage / queues / dashboard | `project_category_state` + `projects.current_stage` | every lane but send_out complete on its last task, send_out active on `submitted`, headline `submitted` / Estimating Admin |

`proposal_sends.draft_id`, `file_id` and `lines_hash` are already nullable, so no
schema change was needed there: these rows simply have no document and no email
(`gc_email` and `email_log_id` null, the same "never emailed" record an external mark
leaves). The Send Out panel shows them as submitted with no file; a re-send of one
fails with the existing "document is no longer on file" message.

RFQs, quotes, BOQ analyses and proposal drafts already in progress are not touched.
They simply stop advancing.

## 7. Atomicity and undo

The backend validates and computes everything (pure `build_plan`), then calls
`apply_external_submission(p jsonb)`, which runs as one transaction: it locks the
project row and the send_out lane row, re-checks eligibility (not PM/CP only, not
declined, not abandoned, past Go/No-Go, not already submitted including a re-verify
bounce), re-checks the send_out head the plan was computed from (compare-and-set; the
backend takes the plan's lanes and that head from one read), no
outcome, no live submission, no send in flight, consumes the grant only if it is
still approved, unexpired and belongs to this user and project, snapshots every row
it will overwrite into `prior_state`, and writes. Any refusal raises
`external_submission:<code>`, which the backend maps to a readable 409 (with an
`X-Error-Code` header) and nothing is written.

Undo (`POST .../undo`, approvers only, optional reason) is refused while a Win/Loss
outcome is recorded (no screen clears an outcome, so undo is final then) or while the bid is not on Submitted
(for example bounced to Verify: commit Verify first). `undo_external_submission`
runs as one transaction: restores the overwritten `proposal_sends` rows, deletes the
ones it created and the send events it added, restores the `project_gcs` overrides,
the `labor_reviews` / `markups` / `verifications` rows (or removes them when none
existed), the lane rows and the headline, deletes the submission's stage events and
every later send_out event (a re-verify bounce and its return), so analytics never
counts an undone submission as a bid date, and marks the submission
`undone` (kept for history). The grant stays `used`.

## 8. API

All under `/projects/{id}/external-submission`, writer roles only
(`require_writer`); the approver / requester split is enforced in the service.

| Method | Path | Who |
| --- | --- | --- |
| GET | `` | the modal's state (role, eligibility, my request / grant, pending requests, live submission, undo) |
| GET | `/pending-count` | badge count (0 for non-approvers) |
| GET | `/wizard` | categories + prefill + GCs (approver, or requester with a valid grant) |
| POST | `` | submit (hourly `outbound_email` rate-limit budget, since it emails every engineer and executive) |
| POST | `/undo` | approvers (same `outbound_email` budget, so a submit/undo loop is capped) |
| POST | `/requests` | requesters (shares the hourly `gc_pricing_request` rate-limit budget, since both fan out to approvers) |
| POST | `/requests/{rid}/cancel` | the requester, pending only |
| POST | `/requests/{rid}/grant` | approvers, pending only |
| POST | `/requests/{rid}/deny` | approvers, pending only, optional reason |
| POST | `/requests/{rid}/revoke` | approvers, unused grants |

Audit actions: `external_submission.requested`, `.cancelled`, `.granted`, `.denied`,
`.revoked`, `.submitted`, `.undone` (labels in `analytics_metrics.ACTIVITY_ACTION_LABELS`).

## 9. Known limits

- The category lines are recorded in `external_submission_lines` and roll into the
  committed snapshot, but they do not create RFQs or quotes. The live "materials" figure
  in the project details box still comes from RFQ selections; the bid price comes from
  the committed snapshot.
- A labor or markup edit after the submission bounces the project to Verify like any
  submitted bid (existing behavior); the re-verify form then seeds from live figures.
- A breakout section recorded here that has no RFQ on the project is in the committed
  basis, but the late-GC pricing editors only offer sections present on the project.
- Release: apply 0140 to staging/prod before deploying this code (the routes and the
  `proposal_sends.origin` column depend on it), then reload PostgREST. No env vars.
