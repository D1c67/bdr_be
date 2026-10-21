# Auto send

Status: built 2026-10-19 on branch `urgent-updates`, migration `0159_auto_send.sql`, DEV ONLY until released with approval.

At 5:05 PM Pacific on a project's internal bid date, if nobody has sent or marked the
proposal submitted, the app emails every GC a proposal priced at the external estimator's
estimate plus a markup (default 10%). The normal Send Out flow is untouched: auto send never
writes `proposal_sends`, `proposal_send_events`, `stage_events` or `project_category_state`
(except through the explicit "Accept as submitted" action, section 9).

All times below are America/Los_Angeles. No em dashes in any user-facing text.

## 1. Owner decisions (2026-10-19)

- Fires at 17:05 on the PT calendar date of `projects.internal_bid_at`, any day of the week.
- Skips the whole project when any GC already has a `proposal_sends` row at `sent` or `sending`
  (emailed or marked submitted), or an active external submission exists.
- Only projects decided Go at Go/No-Go. Declined, pm_only, cp_only, abandoned are silently ignored.
  Projects still in intake or Go/No-Go review are reported as "could not send".
- Sends even when the actual bid time has already passed.
- Price: the estimator workbook's Bid Recap. Materials = material cost + material tax + material
  OH&P; labor = labor cost + labor tax + labor OH&P. Plus `markup_pct` (default 10, Executive and
  IT Admin edit it in Settings). Latest SEALED estimator round only.
- If the estimator who priced the estimate (or any non-revoked assigned estimator) is the dev
  account (`profiles.is_dev`), the project is NOT auto sent (status `skipped`).
- Document = materials, labor, total and the scope lines. No gear / underground / low voltage rows.
- Recipients per GC: the project's GC bid contacts (0110 `project_gc_contacts`, filtered to live
  contacts with an email); otherwise every contact at that GC with an email. A GC with no usable
  email is listed as a failure while the others still send.
- Email: the normal cover note (`proposal_send.build_cover_email` body), sent from the bids
  mailbox with the bids mailbox CC'd (`proposal_send.cc_recipients`). No mention of automation.
  Subject: `[G3 Electrical] Proposal: {name} ({number})` (must differ from the Send Out subject,
  see 6.4).
- Scope lines: the newest `proposal_drafts` row with `status='done'` and non-empty `lines_json`,
  approved or not. A draft is auto generated when an estimator round with a BOQ is sealed and no
  draft exists yet. The Estimating Engineer (Labor) is prompted in the project to complete/approve.
- Notifications go ONLY to active `estimating_admin`, `accountant`, `executive`, `it_admin`
  (engineers excluded on purpose):
  - 15:00 preflight email: what will auto send at 5:05 and what will not, with reasons.
  - ~17:10 digest: one high-importance red email per person, all of today's results
    (Sent, Could not send with reasons, Not auto sent by rule).
- Server down at 17:05: same-day catch-up for `AUTO_SEND_CATCHUP_MINUTES` (default 120). After
  that, or on a later day, never send automatically: the run is recorded `missed` and the
  Estimating Admin gets a notification (bell + email) linking to the project's Auto send panel,
  where Send now pushes it.
- Dashboard / Bids Today / project header show a red pulsing hue on auto sent projects until the
  real proposal is sent or marked submitted (or the auto send is accepted). The project stays
  active and fully editable. The right panel "Auto send" button stays forever and is visible to
  every internal role (accountant read only, estimator never).
- "Accept auto-send as submitted" (Executive, Estimating Admin, IT Admin) records the auto sent
  numbers as the bid.
- A later normal Send Out email to a GC that already got an auto send says "Revised Proposal".
- IT Admin Settings has the on/off switch (DB row, default OFF).

## 2. Safety rails

- DB switch `auto_send_settings.enabled` (default false). Env kill switch `AUTO_SEND_POLLER_ENABLED`
  (default true; tests pin false) stops the poller entirely. The DB switch is re-read before every
  project of a tick and before every GC email (a failed read counts as off), so turning it off
  stops a tick already sending: the GCs not yet emailed end `skipped` with reason `switched_off`.
- Outside production (`settings.is_production` false) GC emails are refused unless
  `AUTO_SEND_GC_REDIRECT` is set; when set, every GC email goes ONLY to that address (no CC, To
  replaced, subject prefixed `[AUTO SEND TEST to <original To>] `). The run records the redirect.
- A project is auto sent at most once ever (any earlier run at `sent` or `partial` blocks later
  runs, even if the internal date moves). A GC whose email in an earlier run (another bid date)
  ended `unknown` (or was cut off at `sending`) starts the new run at `unknown` too (reason
  `earlier_outcome_unknown`), so only Send now with `force_unknown` can email it again. While a
  run of the project for another date is still `sending`, the scheduler leaves the project alone
  (next tick) and Send now answers `busy`.
- Every per-project send is claimed by a unique `(project_id, bid_date)` row before any email, so
  two uvicorn workers can never both send. Per GC, `proposal_sends` is re-checked before the GC
  is claimed AND again immediately before its `send_mail` (after the PDF conversion and checks,
  which can take minutes) so a human send at 5:04 wins. The same check also stops on a Send Out
  email that timed out (`proposal_sends` `failed` with the outcome-unknown error) and on the
  switch being off.
- A run beats a heartbeat (`auto_send_runs.updated_at`, a compare-and-set on `status='sending'`)
  before every GC step. `reclaim_stuck` measures staleness from the newest of the claim time, the
  heartbeat and the GC rows' `updated_at`, so a slow live run is never reclaimed; a worker whose
  heartbeat fails (its run was reclaimed) sends nothing more.
- Graph timeouts (`proposal_send._OUTCOME_UNKNOWN_EXC`) mark that GC `unknown` and are never
  retried automatically.

## 3. Schema (`supabase/migrations/0159_auto_send.sql`)

All tables: RLS enabled + forced, no policies (service role only). End with
`notify pgrst, 'reload schema';`.

```
auto_send_settings (single row, id smallint pk default 1 check (id = 1))
  enabled boolean not null default false
  markup_pct numeric(5,2) not null default 10 check (markup_pct >= 0 and markup_pct <= 100)
  enabled_changed_at timestamptz            -- set whenever enabled flips; missed detection ignores
                                            -- send instants before the latest flip to ON
  updated_by uuid references profiles(id) on delete set null
  updated_at timestamptz not null default now()   (set_updated_at trigger)
  seed: insert (id) values (1) on conflict do nothing

auto_send_runs
  id uuid pk default gen_random_uuid()
  project_id uuid not null references projects(id) on delete cascade
  bid_date date not null                    -- PT date of internal_bid_at the run is for
  status text not null check in ('sending','sent','partial','failed','skipped','missed')
  trigger text not null default 'scheduled' check in ('scheduled','manual')
  reasons jsonb not null default '[]'       -- [{code, message, gc_id?}]
  markup_pct numeric(5,2)
  estimate jsonb                            -- EstimateResult snapshot (section 4)
  material_amount numeric(14,2), labor_amount numeric(14,2), total_amount numeric(14,2)
  draft_id uuid references proposal_drafts(id) on delete set null
  lines_json jsonb, lines_hash text         -- frozen copy of what was sent
  redirected_to text                        -- AUTO_SEND_GC_REDIRECT used, if any
  started_at timestamptz, finished_at timestamptz, sent_at timestamptz (first GC sent)
  triggered_by uuid references profiles(id) on delete set null   -- manual Send now user
  accepted_at timestamptz, accepted_by uuid references profiles(id) on delete set null
  accept_prior_state jsonb                  -- {labor_review, markup, verification} Accept replaced
  created_at timestamptz not null default now(), updated_at (trigger; also the run heartbeat)
  unique (project_id, bid_date)
  index (bid_date), index (project_id)

auto_send_gcs
  id uuid pk, run_id uuid not null references auto_send_runs(id) on delete cascade
  project_id uuid not null references projects(id) on delete cascade
  gc_id uuid not null references general_contractors(id) on delete restrict
  gc_name text not null
  status text not null check in ('pending','sending','sent','failed','unknown','skipped')
  error text
  recipients text                           -- proposal_send.join_recipients(to) form
  cc jsonb not null default '[]'
  file_id uuid references project_files(id) on delete set null   -- the docx (category 'proposal')
  email_log_id uuid references email_log(id) on delete set null
  material_amount numeric(14,2), labor_amount numeric(14,2), total_amount numeric(14,2)
  attempts int not null default 0
  sent_at timestamptz, created_at, updated_at (trigger)
  unique (run_id, gc_id)

auto_send_email_log                          -- claim ledger for the internal emails
  id uuid pk, send_date date not null
  kind text not null check in ('preflight','digest')
  user_id uuid not null references profiles(id) on delete cascade
  created_at timestamptz not null default now()
  unique (send_date, kind, user_id)

auto_send_estimates                          -- parse cache, keyed by the workbook file
  estimate_file_id uuid pk references project_files(id) on delete cascade
  project_id uuid not null references projects(id) on delete cascade
  sha256 text, ok boolean not null, code text, message text, result jsonb not null default '{}'
  parsed_at timestamptz not null default now()

proposal_sends: drop + recreate proposal_sends_origin_check to allow
  ('send_out','external_submission','auto_send'); add column
  auto_send_run_id uuid references auto_send_runs(id) on delete set null.

project_files, estimator_submissions: add column impersonated_by uuid references profiles(id)
  on delete set null. Stamped only by a dev "View as" (X-Impersonate-Estimator) upload or submit,
  whose rows otherwise carry the real estimator's id.

function apply_auto_send_acceptance(p jsonb) returns jsonb   (section 9)
  security definer, set search_path = public, pg_temp; revoke execute from public, anon,
  authenticated (match 0145 style).
```

## 4. Estimate parser (`app/services/auto_send_estimate.py`)

Deterministic, no AI. Contract:

```python
class EstimateError(Exception):            # .code, .message (plain English, no em dash)

REASON_MESSAGES: dict[str, str]           # code -> owner-facing sentence

def pick_estimate_file(sb, project_id) -> dict           # raises EstimateError
    # project_files where category='estimate', estimator_deliverable=true,
    # submission_round not null; the latest sealed round with estimate files must hold exactly
    # one workbook (.xlsx/.xlsm, case-insensitive): none in any round -> no_estimate_workbook,
    # none in the latest round while an older round has one -> newer_estimate_not_workbook (the
    # older price is never used), >1 -> ambiguous_workbooks; no sealed estimate at all ->
    # estimate_not_received. Returns the row (id, filename, storage_path, size_bytes,
    # submission_round, uploaded_by, sha256, created_at, impersonated_by) plus
    # "newer_round_without_workbook": always None now (kept for the wire shape).

def parse_recap(xlsx_bytes: bytes) -> dict                # pure; raises EstimateError
    # returns {"sheet", "components": {mc, lc, mt, lt, tc, ohpl, ohpm, tp, bid_sec, base}
    #          (Decimal), "materials": Decimal, "labor": Decimal, "base": Decimal, "warnings": [str]}
    # Label-matched (never fixed addresses): sheet "Bid Recap & Summary" else first title
    # containing "recap"; header row with the 8 cost headers in the first 30 rows; TOTALS row in
    # the DESCRIPTION column; summary labels BASE BID PRICE / BID SECURITY / TOTAL MATERIAL COST /
    # TOTAL LABOR COST. Cross-checks with tol = max(0.05, base * 0.00001):
    #   mc+lc+mt+lt ~ tc; tc+ohpl+ohpm ~ tp; materials+labor ~ tp; tp+bid_sec ~ base;
    #   summary TOTAL MATERIAL COST == mc and TOTAL LABOR COST == lc when present.
    # base > 0, materials >= 0, labor >= 0. Bid security spread proportionally so
    # materials + labor == base exactly (cents, ROUND_HALF_UP; labor takes the remainder).
    # Codes: unreadable, no_recap_sheet, no_header, no_totals_row, no_base_bid,
    #        no_cached_values, formula_error, unpriced_or_zero, crosscheck_failed.

def load_estimate(project_id, *, sb=None) -> dict         # never raises
    # EstimateResult: {"ok": bool, "code": str|None, "message": str|None,
    #   "file_id", "filename", "round", "uploaded_by", "impersonated_by", "uploaded_at",
    #   "estimator_name",
    #   "components": {k: "123.45" strings}, "materials": "x", "labor": "y", "base": "z",
    #   "warnings": [...]}  (money as strings, JSON safe)
    # Uses auto_send_estimates cache keyed by file id (+ sha256 when present); size guard
    # settings.boq_max_bytes before download (code too_large).

def apply_markup(materials: Decimal, labor: Decimal, pct: Decimal) -> tuple[Decimal, Decimal, Decimal]
    # m = q(materials * (1 + pct/100)), l = q(labor * (1 + pct/100)), total = m + l; q = cents
    # ROUND_HALF_UP. Returned total is ALWAYS the sum of the rounded parts.
```

Prod backup facts this relies on: all 74 real recap workbooks share the layout, all have cached
values, BASE BID PRICE == TOTALS TOTAL PRICE, OH&P 25/25, tax 0, bid security 0. AI Estimating
workbooks are unpriced and must fail with `unpriced_or_zero`.

## 5. Service (`app/services/auto_send.py`)

Imports helpers by name (`from app.services.notifications import notify_role, audit, ...`) so
tests can monkeypatch them on the module. Module-level `get_supabase`, `get_settings`, `_now`.
Sync code only (callers use threads / sync routes).

```python
TZ = ZoneInfo("America/Los_Angeles")

def get_settings_row(sb=None) -> dict      # self-heals the row; on read failure raises
def set_enabled(enabled: bool, actor_id: str) -> dict      # stamps enabled_changed_at; audit
def set_markup_pct(pct: Decimal, actor_id: str) -> dict    # 0..100, 2 dp; audit
    # audit entity "auto_send_settings", entity_id None (uuid column), id in payload

def bid_date_of(project: dict) -> date | None             # PT date of internal_bid_at
def due_projects(day: date, sb=None) -> list[dict]        # projects with internal_bid_at on that
    # PT date, abandoned_at null, current_stage not in SILENT_STAGES
    # ('pm_only','cp_only','declined','submitted','bid_outcome'); paged (_page_all).

def evaluate(project_id: str, *, sb=None) -> dict         # never sends, never writes (except
    # the estimate cache). Returns:
    # {"project_id", "bid_date", "outcome": "ready"|"blocked"|"skipped"|"silent",
    #  "reasons": [{"code","message","gc_id"?}], "silent_reason": str|None,
    #  "markup_pct": "10.00", "estimate": EstimateResult|None,
    #  "amounts": {"material","labor","total","base_material","base_labor"} | None (strings),
    #  "draft": {"id","lines","approved"} | None,
    #  "gcs": [{"gc_id","gc_name","recipients":[...],"source":"bid_contacts"|"all_contacts",
    #           "reason": {"code","message"}|None}]}
    # silent: stage in SILENT_STAGES, abandoned, any proposal_sends sent/sending, active external
    #   submission, gone_out_rule, an earlier run at sent/partial.
    # skipped: dev_estimator: an is_dev assigned estimator or uploaded_by, or a dev "View as"
    #   estimate (the workbook or its round stamped impersonated_by, or for older rows an
    #   audit_log 'impersonation.write' acting as that estimator on this project's paths at or
    #   after the workbook upload).
    # blocked: human_send_unknown (a Send Out email timed out), not_go, no_estimator, estimate
    #   codes, no_scope_lines, missing_project_fields, bad_characters, no_gcs, no_gc_emails (all
    #   GCs unusable), gc_emails_blocked (non-prod without redirect).
    # ready: at least one GC sendable (GCs without email carry a per-GC reason, still ready).

def run_send(project_id: str, bid_date: date, *, trigger="scheduled", actor_id=None,
             force_unknown=False, sb=None) -> dict | None
    # scheduled: INSERT the auto_send_runs claim (status 'sending'); 23505 -> return None.
    # manual: compare-and-set an existing run in ('missed','failed','partial') -> 'sending'
    #   (raise AutoSendError(409) otherwise). Only GCs not already 'sent' are attempted; 'unknown'
    #   GCs only when force_unknown.
    # Then evaluate(); outcome != ready -> record status (skipped / failed) + reasons, return.
    # Render once per GC: proposal_send.build_base_context(project, {"lines_json": lines},
    #   amounts with gear/underground/low_voltage None, includes_generator=False), replace
    #   gc_name, proposal_docx.render_proposal, validate_output(... removed_sections=()) plus an
    #   auto-send check that the PRICING TABLE (proposal_docx._pricing_table) has no section label
    #   (scope lines may legitimately say "Underground"). Store docx as project_files category
    #   'proposal' (filename proposal_docx.build_filename), queue office preview.
    # Per GC: re-check proposal_sends for that GC (sent/sending -> GC 'skipped', stop the run
    #   if any GC was sent by a human); convert_for_send -> validate_pdf_isolation -> send_mail
    #   (to=recipients, cc=cc_recipients(recipients), subject AUTO subject, body =
    #   render_proposal_email(cover body with GC name), PDF attachment, inline logo,
    #   project_id, sent_by=actor_id or None). _OUTCOME_UNKNOWN_EXC -> 'unknown'.
    # Final run status: all attempted sent -> 'sent'; some sent -> 'partial'; none -> 'failed'.
    # Bell rows (one insert, mirror_email True for manual; for scheduled only once that day's
    #   digest already went out, so a late run still reaches email) type auto_send.sent /
    #   auto_send.failed to AUTO_SEND_NOTIFY_ROLES. Audit auto_send.sent /
    #   auto_send.send_failed / auto_send.skipped. A crashed run (run_send's except path) and a
    #   reclaimed run get the same bell.

def record_missed(project_id, bid_date, reasons, sb=None) -> dict | None
    # insert status 'missed' (23505 -> None). The caller notifies.

def reclaim_stuck(sb=None) -> int          # runs at 'sending' with no heartbeat for 15 min
    # (more if the converter timeout is raised): GC rows at 'sending' become 'unknown', run ->
    # partial/failed with reason outcome_unknown, result bell. Returns count.

def project_summary(project_id: str, user) -> dict        # panel GET payload, section 8
def pending_project_ids(project_ids: list[str], sb=None) -> set[str]
    # latest run status in ('sent','partial'), accepted_at null, and no proposal_sends
    # sent row for the project -> drives the red pulse.
def auto_sent_gc_ids(project_id: str, sb=None) -> set[str]  # GCs with auto_send_gcs 'sent'
def accept_as_submitted(project_id, run_id, actor_id) -> dict   # rpc, section 9
def send_now(project_id, actor_id, force_unknown=False) -> dict  # manual run_send on the
    # latest missed/failed/partial run; requires settings enabled. Refuses busy (any run of the
    # project at 'sending'), already_accepted, already_sent (a human send). A malformed project
    # or run id is a 404 (PostgREST 22P02 maps to not found).
```

Reason messages (owner-facing, plain English, no em dashes) live in one dict in `auto_send.py`
and the parser's `REASON_MESSAGES`. Examples:
- not_go: "Go/No-Go has not been decided as Go."
- no_estimator: "No estimator is assigned to this project."
- estimate_not_received: "The estimator has not submitted an estimate yet."
- dev_estimator: "The estimator on this project is the dev account, so it is not auto sent."
- no_scope_lines: "There are no scope lines yet. Add them in the Auto send panel."
- missing_project_fields: "The project is missing its address, labor time, or wage type."
- no_gcs: "No GCs are on this project."
- no_gc_emails: "None of the GCs have a contact with an email address."
- gc_no_email (per GC): "{GC} has no contact with an email address."
- convert_failed: "The PDF converter is not responding, so the proposal could not be attached."
- send_failed (per GC): "The email to {GC} failed: {detail}"
- outcome_unknown (per GC): "The email to {GC} timed out and may or may not have gone out.
  Check the bids mailbox before sending again."
- missed: "The server was not running at 5:05 PM, so this was not sent. Open the project to send it now."
- gc_emails_blocked: "GC emails are blocked outside production. Set AUTO_SEND_GC_REDIRECT to test."

## 6. Integrations in existing code

1. `routers/estimator.py submit_deliverables`: after the general-material call in BOTH branches,
   best effort (try/except, never fails the submit): `auto_send_hooks.on_estimator_round(project_id,
   counts, background)` which (a) warms the estimate cache when `counts.get("estimate")`, and
   (b) when `counts.get("boq")` and the project has NO `proposal_drafts` row at all (or only
   failed ones), starts a system scope-lines generation (`proposal_scope.start_generation(project_id,
   boq_file_id, created_by=None)`, refactored out of `routers/proposals.py start_lines_generation`).
2. `proposal_scope.execute`: when a draft with `created_by is None` finishes `done`, notify
   `Role.ESTIMATING_ENGINEER_LABOR` type `auto_send.lines_review` (bell + email), only while the
   auto send switch is ON (its text says the lines go out at 5:05 PM; a failed switch read counts
   as off). `approve_lines` dismisses `auto_send.lines_review` for the project.
3. `routers/projects.py`: `auto_send_pending: bool = False` on `ProjectOut` and
   `BidsTodayProjectOut`, filled from `auto_send.pending_project_ids(ids)` in `list_projects`,
   `bids_today` and `get_project` (best effort: an error there must not 500 the list).
4. `proposal_send.py`: when a GC is in `auto_sent_gc_ids(project_id)`, the normal Send Out email
   subject for that GC is `[G3 Electrical] Revised Proposal: {name} ({number})`. The
   `_reclaim_stuck_sending` / `_reclaim_stuck_event` match accepts either Send Out subject
   (`send_out_subjects`: normal or revised), since the auto sent set a crashed request saw can
   differ from the reclaiming request's. Filenames unchanged. The auto send subject
   `[G3 Electrical] Proposal: {name} ({number})` differs from both so crash reclaim can never
   mistake an auto email for a Send Out email.
   Re-send of an accepted auto send row (`origin='auto_send'`) validates against the run's frozen
   `lines_json` (the draft stays editable until Accept) and with the auto send section rule
   (`resend_section_kwargs`: no whole-body section label check, `assert_pricing_table_clean` on
   the pricing table instead), because its scope lines may say "Underground".
5. `notification_email.py`: `_TYPE_META` entries for `auto_send.sent`, `auto_send.failed`,
   `auto_send.missed`, `auto_send.lines_review` (no em dashes); `_deep_link` sends `auto_send.*` to
   `/projects/{id}?box=auto_send`.
6. `notifications.py`: `notify_roles(roles, project_id, type_, message, *, mirror_email=False,
   metadata=None)` single insert, estimator excluded.
7. `notification_log.py`: classify emails whose `email_log_id` is in `auto_send_gcs` as type
   `auto_send`, title "Auto send proposal sent to the GC".
8. `routers/files.py`: refuse deleting a file referenced by an `auto_send_gcs` row at `sent`,
   `unknown` or `sending`, or at `pending` while its run is still `sending` (documents are stored
   before the emails go out one by one). `upload_file` stamps `impersonated_by` on a dev "View as"
   upload; `estimator_rounds.create_submission_round(..., impersonated_by)` stamps the round.
9. `main.py`: lifespan task gated on `bidding_enabled and auto_send_poller_enabled and
   supabase_url and ms_client_id`, added to the shutdown tuple; router mounted with `_BIDDING`.
10. `config.py`: `auto_send_poller_enabled: bool = True`, `auto_send_poll_interval_seconds: int = 30`,
    `auto_send_preflight_time = "15:00"`, `auto_send_send_time = "17:05"`,
    `auto_send_digest_time = "17:10"`, `auto_send_catchup_minutes: int = 120`,
    `auto_send_missed_lookback_days: int = 7`, `auto_send_gc_redirect: str = ""`.
    Times validated at boot with `_parse_hhmm`. `.env.example` documents them.
11. `tests/conftest.py`: `AUTO_SEND_POLLER_ENABLED=false`, `AUTO_SEND_GC_REDIRECT=""`.
12. `core/roles.py`: `AUTO_SEND_NOTIFY_ROLES` (EA, accountant, exec, IT), `AUTO_SEND_PCT_ROLES`
    (exec, IT), `AUTO_SEND_ACTION_ROLES` (EA, exec, IT; Send now + Accept). The on/off switch is
    `require_role(Role.IT_ADMIN)`.
13. `tests/test_secreview_route_auth_coverage.py`: register every new route.

## 7. Scheduler (`app/services/auto_send_scheduler.py`)

`poll_once()` (sync) + `polling_loop()` (async, `asyncio.to_thread`, every
`auto_send_poll_interval_seconds`). Each tick, with `local = _now().astimezone(TZ)`:

0. Read settings row; on failure do nothing. `auto_send.reclaim_stuck()`.
   If `enabled` is false: stop (no emails at all).
1. Preflight: `preflight_time <= local < send_time`: projects = `due_projects(today)`; if any
   non-silent, evaluate each (one that raises is listed under will not auto send with reason
   `check_failed`, never dropped), claim `auto_send_email_log (today,'preflight',user)` per recipient
   (upsert ignore duplicates, act on returned rows), send one email each; on send failure delete
   that claim so the next tick retries.
2. Send: `send_time <= local < send_time + catchup_minutes`, and only when the switch was already
   ON at today's send instant (`enabled_changed_at` before it: catch-up is for a server that was
   down, never for a switch that was off; turning it on after 5:05 PM arms the next day): for each
   `due_projects(today)` project with no run row and not silent: `auto_send.run_send(pid, today)`.
   Before each project the switch is re-read (off: stop the tick, nothing claimed) and, before
   the claim, the clock (window closed: stop; the missed check records the rest). A project with
   a run for another date still `sending` is skipped this tick.
3. Missed: for each day in `[today - lookback_days, today]`, when the day's send window has closed
   (day < today, or local >= send_time + catchup): due projects with no run row, not silent,
   `project.created_at` before that day's send instant, and the send instant after
   `enabled_changed_at`: `record_missed`; when inserted, `notify_role(ESTIMATING_ADMIN, pid,
   "auto_send.missed", msg)` (mirrored email carries the deep link).
4. Digest: `local >= digest_time` and (no run for today at 'sending' or local >= digest_time + 30
   min) and at least one run for today exists: claim `(today,'digest',user)` per recipient, send
   one high-importance email each (`importance="high"`), subject
   `G3 BDR · [HIGH IMPORTANCE] Auto send results: {date}`; red band "HIGH IMPORTANCE: AUTO SEND
   RESULTS"; sections Sent (GCs + recipients, materials / labor / total, estimator price, markup
   %), Could not send (reasons, including per-GC failures of partial runs, and missed), Not auto
   sent by rule (dev estimator). Each row links to `/projects/{id}?box=auto_send`. On send failure
   delete the claim (retry next tick).
   Preflight subject: `G3 BDR · [HIGH IMPORTANCE] Auto send at 5:05 PM today: {date}`.
   Recipients: active profiles with role in AUTO_SEND_NOTIFY_ROLES and an email. `send_mail`
   without `project_id`, no CC.

## 8. API (`app/routers/auto_send.py`, mounted with `_BIDDING`, sync `def` handlers)

- `GET /auto-send/settings` (require_internal) -> `{enabled, markup_pct, enabled_changed_at,
  updated_at, updated_by_name, can_toggle, can_edit_pct, send_time, preflight_time}`
- `PATCH /auto-send/settings/enabled` (IT Admin) body `{enabled: bool}`
- `PATCH /auto-send/settings/markup` (Executive, IT Admin) body `{markup_pct: Decimal 0..100}`
- `GET /projects/{id}/auto-send` (require_internal; 404 for unknown project) ->
  ```
  {enabled, markup_pct, send_time, bid_date,
   pending: bool,                      # red pulse state
   latest_run: {id, bid_date, status, trigger, reasons, markup_pct, estimate,
                material_amount, labor_amount, total_amount, lines, lines_hash,
                redirected_to, started_at, finished_at, sent_at, accepted_at,
                accepted_by_name, triggered_by_name,
                gcs: [{gc_id, gc_name, status, error, recipients, cc, file_id, filename,
                       email_log_id, material_amount, labor_amount, total_amount, sent_at}]} | null,
   preview: evaluate() result | null,  # only when no run at sent/partial
   can_send_now: bool, can_accept: bool,
   lines_review: {draft_id, approved, lines_count} | null}
  ```
- `POST /projects/{id}/auto-send/send-now` (AUTO_SEND_ACTION_ROLES) body `{force_unknown?: bool}`
  -> project summary. 409 with a plain message when nothing to send / disabled.
- `POST /projects/{id}/auto-send/accept` (AUTO_SEND_ACTION_ROLES) body `{run_id}` -> summary.

Writer scope-line routes already work at any stage (no backend gate); the Auto send panel reuses
them. Add an abandoned-project 409 to the line routes.

## 9. Accept as submitted (`apply_auto_send_acceptance(p jsonb)`)

Input: `{project_id, run_id, actor_id, expected_send_out_head, lanes: [...], note}` (lanes from
`external_submission.target_lanes`-style helper). One transaction:
lock project + send_out lane; refuse (raise `auto_send_accept:<code>`) when: run not for project,
run not sent/partial, already accepted, any proposal_sends sent/sending, active external
submission, outcome recorded, head changed (stale). Then for every `auto_send_gcs` row at `sent`:
upsert `proposal_sends` (status 'sent', sent_via 'email', origin 'auto_send', auto_send_run_id,
file_id, draft_id, lines_hash, gc_name, gc_email = recipients, email_log_id, sent_at = GC sent_at,
material_amount / labor_amount stamps, section stamps NULL) where status not in
('sent','sending'); insert `proposal_send_events` (initial, email, recipients, email_log_id,
cc_recipients, sent_at); upsert lanes like external submission (all complete except send_out
active at submitted); insert one `stage_events` row to 'submitted' with entered_at = run.sent_at;
set `projects.current_stage='submitted'`; stamp run accepted_at / accepted_by. Python then
dismisses `auto_send.*` and `proposal_send_failed` bells and notifies the `submitted` roles like
`external_submission._after_submit`. Audit `auto_send.accepted`.

Like 0140 the same transaction writes the canonical pricing rows, so the pricing summary,
analytics and a late GC's proposal read the auto sent price as the bid: `labor_reviews`
(labor = the estimator's base labor), `markups` (materials / labor markup pct = the run's
`markup_pct`, amounts = sent minus base, section markups NULL) and a committed `verifications`
snapshot (base materials / labor, those markups, section figures NULL, so `bid_price` =
`total_amount`). Base figures come from the run's frozen `estimate` snapshot (`materials`,
`labor`), else are derived from the sent amounts and the markup. The replaced rows are kept on
`auto_send_runs.accept_prior_state`.

## 10. Frontend

- `components/ScopeLinesEditor.tsx` extracted from `SendOutPanel` section A (behavior identical);
  SendOutPanel renders it. Props `{projectId, canManage, onState?}`.
- `components/AutoSendModal.tsx` (own Modal, `?box=auto_send`): status header (Armed for 5:05 PM /
  Sent / Partly sent / Could not send / Missed / Not auto sent by rule / Accepted / Off); numbers
  (estimator materials / labor, markup %, sent materials / labor / total); per-GC table
  (recipients, status, error, open document via `useFilePreview`); frozen scope lines of the run;
  the live ScopeLinesEditor (writers) with a note that unapproved lines still go out; Send now and
  Accept as submitted buttons (EA / Exec / IT) with confirm dialogs.
  - "Armed" only when the scheduler will still pick the project up (`autoSendSchedule`): an
    internal bid date of today or later, and on the day itself before the send time plus the
    catch-up window. A ready project with no internal bid date, or a past one with no run row,
    reads "Not scheduled" with a plain sentence saying it will not auto send.
  - A run that never went out (missed / failed / skipped) for an EARLIER internal bid date than
    the project's current one is history (`isStaleRun`): the panel leads with the preview for the
    current date and shows the old run as one line. Send now is hidden while the scheduler will
    still send on the new date (Send now would push the old run early and block it).
  - "What was sent" figures only for a sent / partial run. A failed run can carry the amounts of
    an attempt that never reached a GC; the panel then shows the preview figures, which are what
    Send now prices.
  - The Send now confirm reads `GET /projects/{id}/gcs` when it opens and lists
    `sendNowTargets` (mirrors `sendable_gc_ids` over the project's CURRENT GCs, so a GC added
    since a partial run is listed and a removed one is not). The confirm button is disabled when
    the list is empty (only unknown GCs left and the retry box unticked) or unreadable.
  - The editor is read-only once the bid is out (host `linesLocked`: abandoned or Send Out at
    submitted / bid outcome), once the run is accepted, or once the editor's own proposal read
    finds a sent proposal (`lockWhenSent`); that read repeats after every Send now / Accept
    (`refreshKey`).
- `ProjectSideMenu`: `auto_send` item for every internal role, red dot while `pending`.
- Project page: `?box=auto_send` deep link; header badge "Auto sent" (button opening the panel,
  red pulse) while pending; warn Callout for `estimating_engineer_labor` while the newest draft is
  done and not approved and nothing is sent ("Review the scope lines. They go out at 5:05 PM on
  the internal bid date even if not approved."). While `auto_send_pending` (the auto send already
  went out) the same Callout instead asks for the lines of the real proposal
  (`projectPage.autoSend.linesReviewAfterSend`).
- Dashboard + Bids Today: `auto-sent-pulse` row class + "Auto sent" danger Badge while
  `auto_send_pending`.
- `globals.css`: `.auto-sent-pulse` (row background between `--error-soft` and transparent,
  static inset left bar for reduced motion) and `.auto-sent-ring` (badge/button ring), animations
  only under `prefers-reduced-motion: no-preference`.
- Settings: `components/AutoSendSettings.tsx` card for executive + it_admin (On/Off segmented
  control only for it_admin, markup % for both); hides on 404.
- `NotificationsBell`: `auto_send.` types open `?box=auto_send`.
- `lib/autoSend.ts` pure helpers + `lib/__tests__/autoSend.test.mjs`.
- Locales: all 6 (`en, ceb, fil, hi, sw, ur`) under `autoSend.*`, `settings.autoSend.*`,
  `projectSideMenu.items.autoSend`, `dashboard.autoSent`, `bidsToday.statusAutoSent`.

## 11. Release checklist

1. Apply 0159 to staging, then prod WITH APPROVAL.
2. Railway: nothing required (defaults). Leave `AUTO_SEND_GC_REDIRECT` empty in prod.
3. IT Admin turns the switch on in Settings when ready.
