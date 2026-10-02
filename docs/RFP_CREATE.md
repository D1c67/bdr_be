# RFP Ingestion: Project Creation (harvest to bidding project)

Design record for the fifth slice of RFP Ingestion: after the pipeline has
classified, matched and harvested an invitation and found no existing
project, turn what it collected into a bidding project, parked in Go/No-Go
with its GC(s), name, actual bid date and the verified documents, and put
the intake fields nobody could fill on the Estimating Admin's desk. The
same slice locks down project numbering for every user: numbers are
assigned by the app in the `YY.M.NNNN` form the company already uses, with
an optional budgetary `B`, and are never typed again.

Status: design v1, 2026-09-16, decisions taken with the user the same day
(section 1). DEV ONLY. Migration 0130 applies to the dev database only.
Nothing here is released; production numbers are never rewritten.

Naming, used everywhere: setting prefix `rfp_create_` (env `RFP_CREATE_`),
service `app/services/rfp_create.py` (creation), `app/services/
rfp_create_files.py` (document promotion job), `app/services/
project_numbers.py` (numbering, no RFP dependency), `app/services/
project_intake.py` (the missing-intake-fields rule, no RFP dependency),
router `app/routers/rfp_created.py` (prefix `/rfp-created`), migration
`0130_rfp_project_creation.sql`, tables `project_number_counter` and
`rfp_created_projects`, pipeline statuses `create` (pending) and
`created` (terminal) on both `rfp_emails` and `rfp_portal_invitations`,
queue job type `rfp_create_files`, bell types `rfp_create.created` and
`rfp_create.intake_needed`, FE page `/rfp-created` ("Created from RFP Ingestion"),
FE namespace `rfpCreated`.

---

## 1. Decisions locked in (2026-09-16)

| Topic | Decision |
|---|---|
| Number format | `YY.M.NNNN` with an optional trailing `B`: two-digit year, month with no leading zero, a four-digit counter, dots between (`26.9.7204`, `26.9.7204B`). Confirmed against production: latest `26.9.7203`, 57 of 58 rows conform, the counter is global and never resets per month (`26.8.7180` then `26.9.7181`). |
| Counter | One global counter, "highest wins" (production has `7188` entered after `7198`, so "last entered" means the maximum). Advances by one per assigned number, wraps `9999 -> 0001`. Seeded at migration time from that database's own highest existing counter (dev seeds from dev, production seeds from production on release day; dev never has to follow production). |
| Assignment | Fully automatic. The New Project form shows a preview ("next: 26.9.7204") and the server assigns the real number on save, atomically, so two people saving at once get consecutive numbers. Nobody types a number, in the form, in Edit Details, or in a saved draft. |
| `B` | A BUDGETARY marker (not a rebid marker), added so estimating engineers notice a budgetary bid at a glance. A "Budgetary" toggle on New Project appends it at assignment; the same toggle on Edit Details adds or removes it later (a rename of the same number). |
| Existing numbers | Never rewritten, not on dev, not on production. The format rule applies only to numbers the app assigns and to the Budgetary toggle. A legacy number the toggle cannot parse is left alone with a clear refusal. |
| Duplicates | The RFP matcher is the duplicate guard (user decision). This slice adds only harvest-key idempotency: one project per `rfp_harvests` row, so a Procore reminder, addendum notice or second-recipient copy that shares the harvest attaches to the project already created from it. Copies that do NOT share a harvest are caught by the create-time guard in 4.6 (a sibling copy that already created, or a project made minutes ago with the same name for the same GC): it links, it never decides that two DIFFERENT invitations are one project. |
| What is filled | Name, actual bid date, address, bidding link (or "no link"), bid notes, invitation date, GC(s) and bid contact, `is_ngem` for NGEM, and the harvested documents. `created_by` is null (System) on the automatic path, the clicking user on the manual path. |
| What stays empty | Internal bid date, due-from-estimator, due-from-vendors and the nine Go/No-Go rubric answers. The DB allows null; only the manual form's schema required them. Filling them is the Estimating Admin's task (a dashboard task plus one email per project). |
| Go/No-Go | The project is created at `intake` and advanced into `go_no_go` with the `review` entry action, never `score` (an empty rubric scores 0, which would be an automatic No-Go). It waits in the Executive's review exactly like a hand-sent project. |
| Who is told | Executives and Estimating Admins on every creation (bell + mirror email). The Estimating Admin's email lists what is missing. |
| Monitoring page | "Created from RFP Ingestion" (named "Created from RFPs" until 2026-09-23): every project this slice created, newest first, with flags (missing files, no GC, intake incomplete, sender was unauthorized, documents not ingested), filterable by when it was created and searchable (section 8). Estimating Admin, Executive and IT Admin. Clear hides a row; Restore brings it back; both at will. |
| Rollout | Button first: "Create project" on the RFP email detail (every tab) and on the NGEM invitation modal from day one. Automatic creation behind `RFP_CREATE_AUTO_ENABLED` (default false), applying to rows that reach the end of the pipeline after the flag is on. |
| Which rows | Every invitation method (`organic`, `procore`, `pipelinesuite`, `smartbid`, `gc_portal`, `general`, `nonorganic`) and NGEM portal invitations. Rows with nothing harvested still create, flagged "missing files". Sibling followers (`flag_reason = sibling`) never create; they are linked to the leader's project. `merged` and `duplicate` rows never create (the project exists). |
| Files | The ORIGINAL bytes are promoted from quarantine when the sandbox verdict is `verified`, every hazard counter except `uri_links` is zero and the promoted bytes carry no byte marker but `/URI` as a name token (estimators take off from vector PDFs; RFQs and the splitter need them too). Anything else is listed on the card with the reason and not ingested; no rasterized fallback. Legacy `.doc` / `.xls` promote the sandbox's converted PDF instead of the original (legacy binaries can carry macros, the converted PDF cannot); a `.docx` / `.xlsx` original is scanned as an OOXML container first and falls back to the converted PDF when the scan refuses it (section 5). |
| GC | A resolved GC attaches with its resolved contact. Unresolved: the card shows "Sender: Name <address>, likely <GC>" when the app can infer one (a GC contact on the sender's domain, or the match step's GC candidates); a `nonorganic` row (an unauthorized sender a user allowed) with no inference creates a new GC and contact from the sender; every other unresolved case creates the project with no GC and the flag. NGEM projects have no GC by design (the agency is the owner). |
| Unauthorized marker | A project created from a row that was `flagged_unauthorized` and continued by a person carries "sender was unauthorized, allowed by <user>" on the page and on the project header. |
| Name-less rows | "Set project name" on the email detail (with an Are-you-sure modal) writes the name and sends the row back through `match`, so it harvests and creates like any other. |
| Out of scope | PM-only and Certified Payroll project creation still take a typed number (other sub-apps; the counter skips any number they take). Automatic re-harvest for addenda. Making the sandbox's reading tier lazy now that originals reach the project (follow-up, storage win only). |

---

## 2. Numbering (`app/services/project_numbers.py`, migration 0130 part 1)

```
project_number_counter (id smallint pk check (id = 1), last int not null check (last between 0 and 9999), updated_at)
next_project_number() returns int   -- one UPDATE ... RETURNING; the row lock serializes callers
```

Seed, in the migration: `insert ... select coalesce(max(counter), 0)` over
`regexp_match(btrim(number), '^\d{2}\.\d{1,2}\.(\d{4})B?$')` on `projects`,
`on conflict do nothing` (re-runnable; a second apply never moves the
counter). `last = 0` on an empty database, so the first number is `0001`.
The function is `security definer`, execute revoked from `public`, `anon`
and `authenticated`: only the service role (the backend) calls it.

Python API (pure except where `sb` is passed):

```python
NUMBER_RE = re.compile(r"^(\d{2})\.(\d{1,2})\.(\d{4})(B?)$")
def is_valid(number: str) -> bool
def parse(number: str) -> ParsedNumber | None          # (yy, month, counter, budgetary); btrim first
def format_number(yy: int, month: int, counter: int, *, budgetary: bool) -> str
def prefix_for(now_utc: datetime) -> tuple[int, int]   # Pacific-time year % 100 and month (COMPANY_TZ)
def preview(sb) -> str                                  # reads the counter row, formats last+1 (wrap-aware); NEVER advances
def assign(sb, *, budgetary: bool) -> str               # rpc next_project_number, format; retried below
def with_budgetary(number: str, budgetary: bool) -> str # add/strip B; raises ValueError on a legacy number
```

`insert_with_assigned_number(sb, payload, *, budgetary)` loops: take the
next counter, format, insert into `projects`; on a unique violation (a
PM-created or legacy number already holds it) it asks again, at most
`NUMBER_MAX_TRIES` (20) times, then raises `NoFreeNumberError` (409 "No
free project number could be assigned; try again").
The counter is never rewound, and never bumped from a typed number (nothing
types one anymore).

Router changes (`app/routers/projects.py`, `app/models/schemas.py`):

- `ProjectCreate.number` is REMOVED; `ProjectCreate.budgetary: bool = False`
  is added. The schema ignores an unknown `number` key from an old client
  (pydantic default `extra = "ignore"`), and the server assigns.
- `create_project` assigns inside the insert loop above; the response carries
  the assigned number as today.
- `ProjectUpdate.number` is REMOVED; `ProjectUpdate.budgetary: bool | None`
  toggles `B` through `with_budgetary`; a legacy number answers 409
  `"This project's number predates automatic numbering and cannot be changed"`.
  Everything else in `update_project` is untouched (the bid-date email, the
  URL xor rule).
- `GET /projects/next-number` -> `{"number": "26.9.7204", "budgetary_number": "26.9.7204B"}`
  for the form's preview (writer roles; rate limited like `/similar`).
- `bid_drafts.number` becomes nullable (migration); `POST/PUT /bid-drafts`
  no longer require it, `transfer` ignores it; the list keeps returning it
  for old drafts.
- `services/pm.py` and `services/payroll_projects.py` still take
  `body.number` (out of scope, section 1).

The unique index `projects_number_unique_idx` on `lower(btrim(number))`
(0052) stays the backstop.

---

## 3. Pipeline

```
... -> match
        |  no existing project
        |    harvester + reference -> harvest -> (job) -> create
        |    otherwise                          -> create
        v
   create     step: FIRST the link-only path, flag on or off: a harvest that
              already has a project links the row -> created
              then RFP_CREATE_AUTO_ENABLED off -> done (the button takes over)
              on  -> rfp_create.create_from_email(sb, row, actor_id=None) -> created
              a losing claim (another worker is creating the same harvest) waits
              RFP_CREATE_POLL_SECONDS (30) and re-checks
        v
   created    terminal: created_project_id set; the card shows the project
```

Status vocabulary after this slice, `rfp_emails`: pending gains `create`;
terminal gains `created`. `done` keeps meaning "processed, no project,
nothing created" and is where the manual button acts. `set_method` keeps
accepting `create`/`created` (both are after `method`); `dismiss` refuses
both; the reopen route keeps refusing `created` (unmerge is not a concept
here; a created project is abandoned or discarded from the project itself).

Where rows enter `create` (all in existing seams, replacing `done`):

1. `rfp_email_ingest._park_done`: the match step's "no project" exits
   (`no_project_name` included: the row lands at `create`, the step sees no
   name and drains to `done` with `flag_reason` untouched, so "Set project
   name" can send it back through `match`).
2. `rfp_harvest._finish_email` in pipeline mode (the job's own exit, success
   and permanent failure alike: a failed harvest still creates, flagged
   "missing files").
3. `rfp_harvest.mark_from_queue` ladder exhaustion (same function).
4. Sibling followers: unchanged, they go `extract -> done` with
   `flag_reason = sibling`; the leader's creation links them (section 4.4).
5. `reject_match` ("Not a match" on a `review_match` row) takes the same
   road as exit 1 (`_park_target`: `harvest` when the method has a
   harvester and the email carries something for it, else `create`), CAS
   from `review_match`, with the `match_review_*` fields written,
   `attempts = 0`, `flag_reason` untouched and the candidates kept. A
   rejected match harvests and creates like any other row instead of
   parking at `done`.

One emptiness rule for the project name, everywhere: `rfp_create.
has_project_name(row)` (the name survives `rfp_match.normalize_project_name`;
"Invitation to Bid" and "RFP 2026-01" are no name). The match step's
`no_project_name` exit, `_step_create`, the service preconditions and
`email_create_available` all use it, and `set_project_name` refuses a name
that normalizes to nothing (409 with a sentence) and a sibling follower
(409: "This copy follows another email; set the name on that one.").

`_step_create(sb, row) -> str | None` in `rfp_email_ingest.py`:

0. Link-only path: the row's harvest already has a `project_id` -> CAS
   `create -> created` with that project, flag on or off, name or no name
   (a reminder or a second copy of an invitation whose project exists).
1. `settings.rfp_create_auto_enabled` is false -> `_terminal(create -> done)`,
   `decided_at_step = "create"`, flag untouched. Return None.
2. Row has no project name (`has_project_name`) -> same drain to `done` (a
   name is the one thing creation cannot invent).
3. `rfp_create.create_from_email(sb, row, actor_id=None, automatic=True)`:
   - returns `Created(project_id, linked=False)` -> the service already
     CASed `create -> created`; return None.
   - returns `Created(project_id, linked=True)` (the harvest already had a
     project) -> same.
   - raises `CreateInProgress` -> `_cas` `next_attempt_at = now + poll`,
     no attempt spent; return None.
   - any other exception -> `_retry_or_fail(step="create")` (1 min, 5 min, then 15 min,
     then `failed` with `create_error`).

NGEM (`rfp_portal_ingest.py`) mirrors it: `_finish_invitation` targets
`create`; `_process_invitation` dispatches `create` to `_step_create`
(the same link-only path first, then the same three outcomes,
`rfp_create.create_from_portal`); statuses `create`
and `created` join `STATUS_PENDING` / `STATUS_TERMINAL` and the DB CHECK;
`resolve_new`, `reopen`, `ignore` keep their current guards and refuse
`created`; `ignore` accepts `create` (a row waiting there for the flag, a
claim or a retry; a creation in flight loses its final CAS and compensates).

Deleted projects (migration 0141, docs/PROJECT_DELETE.md 5): when a project
this slice made is deleted, its source rows and harvest carry
`create_blocked_archive_id`. Both create steps check
`rfp_create.create_block_for` right after the link-only path (the row, its
harvest, a copy or package sibling, and for an email a deleted project the
matcher would have linked it to) and drain a blocked row to `done` with
`flag_reason = project_deleted`; the service refuses it on the button path too
(`CreateBlocked`, a `CreateRefused`). A restore lifts the marks.

---

## 4. The service (`app/services/rfp_create.py`)

```python
@dataclass(frozen=True)
class Created:
    project_id: str
    number: str
    linked: bool          # True when the project already existed (no new project): the
                          # harvest carried it, or the 4.6 guard found it
    files_job: bool       # True when a promotion job was enqueued

class CreateInProgress(Exception): ...   # another worker holds the harvest's create claim
class CreateRefused(Exception): ...      # user-facing sentence (no name, wrong status, sibling, merged)

def create_from_email(sb, row: dict, *, actor_id: str | None, automatic: bool) -> Created
def create_from_portal(sb, inv: dict, *, actor_id: str | None, automatic: bool) -> Created
def facts_for_email(row: dict, harvest: dict | None) -> Facts          # pure
def facts_for_portal(inv: dict, harvest: dict | None) -> Facts         # pure
def gc_plan_for(sb, row: dict, harvest: dict | None) -> GcPlan          # resolved | likely | create | none
def link_harvest_mates(sb, harvest_id: str, project_id: str) -> int    # other rows sharing the harvest -> created
def normalized_name(value) -> str                                       # pure: whitespace collapsed, casefolded
def duplicate_project_for(sb, row: dict, settings) -> _Duplicate | None
    # 4.6: _Duplicate(project_id, why, sibling_email_id, project) or None
def recent_project_sentence(project: dict | None) -> str                # the manual path's refusal
```

### 4.1 Preconditions (`CreateRefused` with a sentence)

Email row: status must be `create` (pipeline) or `done` (manual button);
`flag_reason != sibling` (that row is another copy of the same message; the
project is created from the copy that leads the group); `has_project_name(row)` (section 3's one rule);
not already `created_project_id`. Portal invitation: status `create` or `done`; `title`
non-empty. A manual call on a row whose harvest already has a project
links the row and returns `linked=True` (HTTP 200 with the existing
project, the FE says "already created from this invitation").

### 4.2 Claim (idempotency without a similar-name check)

When the row has a `harvest_id`: CAS `rfp_harvests.create_claim_token`
from null (or a stale claim older than `RFP_CREATE_CLAIM_SECONDS`, 600)
to a fresh token, `create_claimed_at = now`, fenced on `project_id is
null`. A miss with `project_id` set means "already created": link and
return. A miss with a live claim raises `CreateInProgress`. When the row
has no harvest there is no shared object to claim, and the source row's
own CAS at the end of creation (`create|done -> created`, step 6 of 4.5)
is the fence: two workers cannot both win it, and the loser compensates
like `_compensate_lost_race` does (delete the project it just inserted,
cascades clean the links, audit `rfp_create.lost_race`). The same
compensation covers a harvest claim whose row CAS is lost.

### 4.3 Facts (pure)

| project column | email source (in priority order) | portal source |
|---|---|---|
| name | `extracted_project_name` when `extract_model = "human"` (a person typed it: "Set project name"); else harvest `data.project_name` (Procore, PipelineSuite, SmartBid) else `extracted_project_name`; trimmed, capped 200 | `title` |
| actual_bid_at | harvest `data.bid_due_at` when an instant; else `extracted_bid_due_at`; a date-only value (harvest `YYYY-MM-DD`, or `extracted_bid_due_has_time = false`) is stored at midnight Pacific and "bid time" joins the missing list | `close_at` |
| invitation_at | `received_at` | `first_seen_at` |
| address | harvest `data.project_address` | null |
| bidding_url / no_bidding_url | harvest `external_url` (Procore bid sheet, PipelineSuite portal, SmartBid project list); else the first `data.links[].url` with status `listed`/`needs_sign_in` (email harvester); else `no_bidding_url = true`. Cleaned by `_clean_bidding_url`; a URL it refuses falls back to `no_bidding_url` | `no_bidding_url = true` (the portal URL is session-bound) |
| bid_notes | `extracted_bid_notes`, then harvest `instructions_text`, then `description_text`, joined by a blank line, capped `RFP_CREATE_NOTES_MAX_CHARS` (4000) | harvest `description_text` |
| notes | one line: "Created from RFP invitation <subject> received <date> (<method>)" | "Created from NGEM <agency> bid <bid_number_raw>" |
| is_ngem | false | true |
| is_rebid | false (`possible_rebid_project_id` is shown on the card, never applied) | false |
| created_by | actor_id (manual) or null | same |
| current_stage / owner | `intake` / Estimating Admin, then advanced (4.5) | same |

Everything else (internal date, the two due dates, labor, wage, the nine
rubric answers, est dates) is null.

### 4.4 GC plan

1. `resolved_gc_id` set -> `GcPlan(kind="resolved", gc_id, contact_id=resolved_gc_contact_id)`.
   Attach: `project_gcs {project_id, gc_id, needs_by: actual bid date's Pacific day}` and
   `project_gc_contacts` for the contact when present.
2. Else infer "likely": the top entry of `gc_candidates` (name, gc_id when
   present), else a `gc_contacts` row whose email domain equals the sender's
   domain (public mailbox domains skipped, `rfp_email_auth.is_public_mailbox_domain`)
   -> `GcPlan(kind="likely", gc_id?, name)`. The project gets NO GC; the card
   says "Sender: Name <address>, likely <GC>". (A likely GC is a person's call:
   one click on the project's GC panel adds it.)
3. Else if `invitation_method == "nonorganic"`: the GC name is the harvest
   `data.gc.name`, else `extracted_gc_name`, else the sender's display name
   or domain (`directory.clean_company_name`). `find_duplicate_company`
   decides between two outcomes:
   - an EXISTING GC matches on a near-duplicate name -> it is REUSED as is:
     attached as the project GC with NO contact (an outside sender's
     address is never added to a real GC's contacts by the app), and the
     record says `gc_plan = likely` with `likely_gc_id` / `likely_gc_name`
     naming it (the card shows "likely <GC>"; a person confirms the contact);
   - no GC matches -> a `general_contractors` row is inserted, plus a
     `gc_contacts` row from the sender (`from_name`, `from_address`), and
     both are attached; `gc_plan = created` means exactly "rows this call
     inserted". A compensation (4.5) deletes those two rows again.
4. Else `GcPlan(kind="none")`: no GC, flag "no GC".

Portal: always `none` (NGEM), no flag.

The likely-by-domain lookup (step 2) filters `gc_contacts` server-side
(`email ilike '%@<domain>'`, the domain escaped as a LIKE literal), caps
the read at 500 rows and re-checks the exact domain in Python.

Unauthorized marker: `sender_was_unauthorized = true` only when the row's
`continued_by` is set or its audit trail shows `rfp_email.continue` (the
row passed through `flagged_unauthorized` and a person let it continue);
`sender_allowed_by` = `continued_by`, else that audit row's actor. The
method alone never counts: a `nonorganic` set by hand on a row that was
never flagged is a correction, not an override. `sender_display` =
`"{from_name} <{from_address}>"` always.

### 4.5 Creation, in order (compensating delete on any failure after step 1)

1. `project_numbers.assign` + `projects.insert` (section 2 loop).
2. `rfp_created_projects` row (section 6), right behind the project, so a
   crash in the steps after it never leaves a project the page cannot see.
3. GC links (4.4); a reused GC updates the record to `gc_plan = likely`.
4. `stage_events {from None -> intake, category intake, actor_id}`,
   `project_category_state` seed (same rows as `create_project`).
5. `workflow.advance_category(project_id, "intake", actor_id, note="Created from RFP")`
   then `gono.apply_entry_action(project_id, actor_id, "review")` (returns
   `(None, None)`: parked in review). The headline becomes `go_no_go` /
   Executive.
6. Source row CAS: email `create|done -> created`, `created_project_id`,
   `decided_at_step = "create"`, `next_attempt_at = null`, `last_error = null`;
   portal the same on `rfp_portal_invitations`. A miss -> compensate (4.2).
7. `rfp_harvests.project_id = project_id`, claim released; `link_harvest_mates`:
   every other `rfp_emails` row with the same `harvest_id` at `done` or
   `create` (and every sibling follower whose `sibling_of_email_id` is this
   row, AT ANY DEPTH) -> `created` with the same `created_project_id`
   (`flag_reason` untouched). The follower walk is transitive and reads each
   level before it writes it, because a copy that followed a copy leaves a
   chain (docs/RFP_MATCHING.md 3.1) and one hop would strand its far end
   with `flag_reason = sibling` and no project; it is bounded at 8 levels
   and cycle-safe. Portal mates likewise.
8. Files: when the harvest has accepted or reused entries with a
   `sandbox_file_id`, CAS `files_status none -> pending` FIRST
   (`rfp_create_files.mark_pending`), then enqueue `rfp_create_files`
   (section 5); an enqueue that fails puts `none` back. A crash between the
   two leaves a pending record "Retry documents" can act on, never a job
   over a record that still says none. Otherwise `files_status = none` and
   the "missing files" flag is on.
9. Notifications (section 7), audit `rfp_create.create` on the project with
   `{source_kind, source_id, harvest_id, number, automatic}`.
10. `email_ingest.rescan_unknown_for_project(project_id)` in a daemon thread
    (the learn-back `create_project` runs as a background task), best effort.

Steps 1 to 4 are the atomic core (compensated as `create_project` does:
delete the project row, cascades clean the children, the GC and contact
this call inserted are deleted again, audit the compensation).
Steps 5 to 10 are best effort after the project exists: a failure there
logs, is recorded on `rfp_created_projects.last_error` (surfaced on the
page as the "Creation error" flag), and never deletes the project.

### 4.6 Create-time duplicate guard (email rows)

The harvest claim (4.2) only fences copies that SHARE a harvest. Two copies
of one invitation with their own harvests, or with none, can both reach
`create`, so the last read before the insert in step 1 is a duplicate check.
Both halves run for an email row on either path (the pipeline's `create`
step and the "Create project" button); a portal invitation has no sender, no
subject and no copies, so it is skipped.

1. **Sibling already created.** The copies of this row, read in both
   directions inside `RFP_MATCH_SIBLING_WINDOW_MINUTES` with the same
   sibling key and GC identity (docs/RFP_MATCHING.md 3.1). The oldest one
   carrying a `created_project_id` wins, so two copies racing here land on
   the same project. A copy at `created` with no project id has nothing to
   join and is skipped.
2. **A project made moments ago.** A `projects` row created within
   `RFP_CREATE_DUPLICATE_WINDOW_MINUTES` (60; 0 disables this half), not
   abandoned, whose name equals this row's `extracted_project_name` with
   whitespace collapsed and casefolded, AND already linked in `project_gcs`
   to this row's `resolved_gc_id`. The oldest such project wins. A row with
   no `resolved_gc_id` skips this half entirely: a name on its own is the
   matcher's evidence, not a blind guard's.

Neither half ever links to a project in the row's `excluded_project_ids`
(an unmerge put it there precisely so this row never lands on it again), to
a project that has been abandoned or deleted, or to a project outside the
row's test-bench scope: `read_siblings` and the projects read both filter on
`test_session_id` the way the sweep does, so a test row only ever joins a
test project and a real row only a real one.

A hit LINKS instead of creating: the source row CASes `create|done ->
created` with that `created_project_id` and `decided_at_step = create` (a
sibling hit also stamps `sibling_of_email_id` and `flag_reason = sibling`,
the same marks 3.1 and `link_harvest_mates` leave), a `rfp_create.link`
audit row records what was duplicated in `duplicate_of` (the copy's email id
for a sibling hit, the project id for a recent-project hit) and the rule that
found it in `why` (`sibling` or `recent_project`), and the call returns
`Created(linked=True, why=..., duplicate_of=<email id or null>)`. A test row records a `create` / `linked` event; other rows log at
info. The one thing that can raise is the CAS: a miss is the lost race and
raises `CreateRefused(_MSG_LOST_RACE)`, exactly as `_link_existing` does.
No `rfp_created_projects` row, no bells and no rescan either way: the
project already has all of those.

The two halves differ on the harvest and the documents:

- **Sibling hit.** The copy that made the project is the same invitation, so
  its harvest is this row's harvest (or a reuse of the same external key)
  and its documents have already been linked and promoted. Nothing more is
  done: the claim is handed back with no `project_id`, and `files_job` is
  false.
- **Recent-project hit.** A DIFFERENT invitation made that project, so this
  row's own harvest still holds documents nobody has promoted. Steps 7 and 8
  run against the project that exists: `rfp_harvests.project_id` is set, the
  claim released, the split job and the harvest mates linked, and the
  `rfp_create_files` job enqueued. Two things differ from step 8 on a fresh
  project, because the promotion job resolves its work from the TARGET's
  `rfp_created_projects` row, not from this one's harvest:
  - the job payload NAMES this row's harvest (`{project_id, harvest_id}`),
    and that harvest wins over the record's inside `execute` (section 5), so
    the link promotes this invitation's documents rather than promoting the
    other invitation's a second time; the split job comes from the same
    harvest, falling back to the record's copy of it;
  - a target project with no record at all (a project made through New Bid)
    gets one, keyed to THIS harvest, with `gc_plan = none` (this call
    attached no GC): without it the `files_status` CAS matched nothing, the
    job was never enqueued, and the documents were dropped with
    `rfp_harvests.project_id` already set, so nothing later picked them up.
    The record is also what gives the promotion a card, a status and a
    "Retry documents" button. A target that already has a record keeps it
    untouched; the fence CASes from whatever `files_status` it carries
    (`none`, `complete` or `failed`). While that record is `pending` or
    `running` the queue allows no second job for the project, so nothing is
    enqueued and the card's `last_error` says to use "Retry documents" once
    the run in flight finishes.

On the MANUAL path (the "Create project" button, `automatic=False`) the
recent-project half does NOT link. A person's click is never attached to a
project the app only inferred: it raises `CreateRefused` with a sentence
naming the project and its age ("A project with this name for this GC was
created 7 minutes ago: 26.9.7124 NAME. Merge this email into it, or rename
the project, before creating another."), which the router returns as the 409
`rfp_create_refused` it already returns for refusals. The sibling half links
on both paths: a copy of the same message is not an inference, it IS this
email. The automatic path links on both halves.

Uses existing columns only, no migration.

---

## 5. Document promotion (`app/services/rfp_create_files.py`, job `rfp_create_files`)

Runs in the queue (third claim pass beside `rfp_harvest`, concurrency 1 per
worker, priority `rfp_create_files_queue_priority` 160) because a 100-file
set is minutes of streaming. Payload `{project_id, harvest_id}`, where
`harvest_id` names the harvest to promote and wins over the record's; it is
omitted (and the record's harvest used) everywhere except 4.6's
recent-project link, whose harvest is not the target record's. The job:

1. Reads `rfp_created_projects` (claim `files_claim_token` like the harvest
   claim; `files_status = running`; a claim that cannot be taken raises the
   transient error, so the ladder retries and its exhaustion marks the
   record `failed`, never stranding `running`), the harvest's `files[]`,
   and each referenced `rfp_ingest_files` row (`status, hazards,
   quarantine_path, source_format, converted_path, filename, size_bytes,
   sha256, manifest`).
2. Per entry, decision (pure, `promotion_for(entry, file_row) -> Promote | Skip`):
   - no `sandbox_file_id` or entry status not in (accepted, reused) -> Skip(reason = entry status: rejected / too_large / download_failed / skipped_cap / expanded)
   - file status `verified_with_gaps` -> Skip("pages_unverified"); `rejected`/`failed`/`pending`/`running` -> Skip(that status)
   - `hazards` not a dict -> Skip("hazards_unknown"): an unknown hazard block is never clean
   - any PDFium hazard counter > 0 other than `uri_links` -> Skip("hazard:<keys>")
   - (the byte markers are checked on the downloaded bytes in step 3, not from the manifest: the sniff's `byte_markers` is a substring count, and `/AA` matches the `/AAPL:Keywords` every Mac-made PDF carries)
   - `source_format` in (pdf, docx, xlsx): no `rfp_ingest_files.sha256` -> Skip("no_digest"); else Promote(original from quarantine, `source_content_type`, `expected_sha` = that digest; docx/xlsx carry `ooxml_scan`)
   - `source_format` in (doc, xls) -> Promote(`converted_path` from the derived bucket as `<stem>.pdf`, "application/pdf", note "Converted from the original .doc by the ingestion sandbox", `expected_sha` = `manifest.conversion.pdf_sha256` when present; absent -> the note gains `converted_unverified`)
3. Fetch and check (`_fetch_entry`): `rfp_ingest_storage.download_to_file`
   (max `upload_max_bytes`) to scratch; the digest of the bytes must equal
   `expected_sha` (original: Skip("changed_since_verification"); converted:
   Skip("converted_sha_mismatch"); a converted PDF with no recorded digest
   is promoted as noted). Every PDF's bytes are then scanned for
   `protocol.BYTE_MARKERS` as PDF NAME TOKENS (`pdf_marker_keys`: the
   marker followed by whitespace, a delimiter or the end of the file);
   any marker but `/URI` -> Skip("marker:<keys>"). This scan is the
   authoritative net behind PDFium's counters (which cannot see a catalog
   `/OpenAction` or a widget action): a name written with `#xx` escapes is
   decoded before matching, and every stream is found the way a reader
   finds one: from each `N G obj` header, its dictionary parsed by a small
   PDF tokenizer (strings, hex strings, comments, nesting and `#xx` escapes
   honoured; never a byte window before the keyword). Every plain
   `/FlateDecode` stream is inflated in chunks (per stream
   `PDF_INFLATE_STREAM_CAP` 256 MB, per file `PDF_INFLATE_TOTAL_CAP` 4 GB)
   and scanned the same way, so a catalog hidden in a compressed object
   stream is seen. The scan fails closed, Skip("unscannable:<reason>"),
   never promoted (`PdfUnscannable`), on: a `stream` keyword (any line
   ending: `\r\n`, `\n`, a bare `\r`, trailing blanks) that no parsed
   object accounts for (`stream_unparsed`), or with junk on its line
   (`stream_eol`); an object stream (`/Type /ObjStm`, a `/Type` given
   indirectly, or any stream carrying `/N`, which is how pdf.js and MuPDF
   load one without checking `/Type`) behind any other filter, a filter
   chain or an indirect filter (`objstm_filter`), a predictor
   (`objstm_decode_parms`), an unsettled length (`objstm_length`) or a
   deflate stream that ends early (`objstm_truncated`); a corrupt or
   over-cap stream (`stream_corrupt`, `stream_too_large`,
   `streams_too_large`); and a file past the tokenizing budget
   (`PDF_PARSE_OBJECT_CAP` 4 MB per object, `PDF_PARSE_BUDGET` 128 MB per
   file: `scan_budget`). A docx/xlsx original then goes through
   `ooxml_container_verdict` (below); a refusal falls back to the converted
   PDF exactly as doc/xls, downloaded and digest-checked in turn, with the
   note `Converted from the original .docx by the ingestion sandbox;
   converted:<why>` (no converted copy -> Skip("ooxml:<why>")).
4. Promote: `storage.upload_file(build_object_path(project_id, category,
   filename), bytes, mime)`, insert `project_files` with `uploaded_by = null`,
   `mime_type`, `size_bytes`, `preview_status` per `office_preview.is_convertible`,
   `rfp_harvest_id`, `rfp_sandbox_file_id`; the partial unique index
   `(project_id, rfp_sandbox_file_id)` makes a retry idempotent (a unique
   violation reads as "already promoted", the uploaded object is deleted).
   `office_preview.generate_preview` is called inline for convertible files.
   Audit `file.upload` with `{"category", "source": "rfp"}` per file; ONE
   `_notify_drawing_changed`-equivalent bell at the end when any drawing
   was promoted, not one per file.

The OOXML container scan, `ooxml_container_verdict(path_or_file) -> str | None`
(pure; None = clean, else the reason): the archive's end record first
(`rfp_zip.precheck`, before `zipfile` parses the directory: `too_many_members`
above 5000 declared entries, `central_directory_too_large` over a 4 MB
directory), then the `zipfile` listing, then `[Content_Types].xml` and every
`*.rels` member inflated in 64 KB chunks under a running counter
(`rfp_zip.read_member_bounded`: the declared size is never trusted). Refuses
with `too_many_members` above 5000 members and `member_too_large` when an
inspected member is over 2 MB declared, compressed or actually inflated; `bad_zip` when the container
cannot be opened. The inspected members are parsed as XML
(`rfp_zip.relationships`, `rfp_zip.flattened_xml`: expat over the bounded
bytes, entities decoded, any encoding the part declares, prefixes
tolerated), so an entity-encoded attribute, a prefixed element or a UTF-16
part reads the way Word reads it; a member that is not well-formed XML or
declares a DTD refuses the original (`bad_xml`). Fails on: a member named
`vbaProject.bin` (`vba_project`); any member under `*/embeddings/` or named
`oleObject*` (`embedding`); any other `.bin` under `word/` or `xl/`
(`binary_part`); `xl/externalLinks/` (`external_link`); a `.rels`
relationship whose TargetMode reads External, or whose Target is a URL or
UNC path, of a type containing attachedTemplate, oleObject, frame,
externalLink or package (`external_rel:<word>`); `ddeLink` or `DDEAUTO`
anywhere in an inspected member, parsed or raw (`dde`);
`[Content_Types].xml` declaring `macroEnabled` (`macro_enabled`). Names
are compared case-insensitively.
5. Category: Procore `kind = drawing` and `discipline` containing
   "electrical" (case-insensitive) -> `electrical_drawing`; `drawing` ->
   `drawing`; `specification` -> `specification`; anything else, and every
   email-harvester or NGEM entry (kind null) -> `other`.
6. Filename: the entry's `file_path` basename, sanitized by
   `storage.safe_key_component` for the key and kept readable for
   `project_files.filename`; a name collision within the project is allowed
   (uuid prefix in the key).
7. End: `files_status = complete`, `files_promoted`, `files_skipped`
   (`[{file_path, reason}]`, capped 200 entries), `files_error = null`. A
   transient failure (storage) releases the claim and requeues through the
   queue ladder; ladder exhaustion writes `files_status = failed` +
   `files_error`. "Retry documents" on the page re-enqueues: it accepts
   `none`, `failed`, and a `running` whose `files_claimed_at` is older than
   `llm_queue_lease_seconds` (`rfp_create_files.retryable`), CASes the
   record to `pending` from the status it read (`mark_pending`) BEFORE the
   enqueue, and puts the prior status back when the enqueue fails
   (`unmark_pending`).

Never promoted: images (never in the sandbox), attached emails, zip
containers (their members are entries of their own), anything the sandbox
did not verify.

---

## 6. Data model (migration `0130_rfp_project_creation.sql`)

```
project_number_counter   (section 2) + next_project_number()
bid_drafts.number        drop not null

rfp_created_projects
  project_id            uuid pk references projects(id) on delete cascade
  source_kind           text not null check in ('rfp_email', 'rfp_portal')
  rfp_email_id          uuid references rfp_emails(id) on delete set null
  portal_invitation_id  uuid references rfp_portal_invitations(id) on delete set null
  harvest_id            uuid references rfp_harvests(id) on delete set null
  created_by            uuid references profiles(id) on delete set null   -- null = System
  automatic             boolean not null default false
  invitation_method     text
  sender_display        text
  sender_was_unauthorized boolean not null default false
  sender_allowed_by     uuid references profiles(id) on delete set null
  gc_plan               text not null check in ('resolved', 'likely', 'created', 'none')
  likely_gc_id          uuid references general_contractors(id) on delete set null
  likely_gc_name        text
  bid_time_unknown      boolean not null default false   -- actual_bid_at came as a date only (4.3)
  files_status          text not null default 'none' check in ('none', 'pending', 'running', 'complete', 'failed')
  files_claim_token     text
  files_claimed_at      timestamptz
  files_promoted        int not null default 0
  files_skipped         jsonb not null default '[]'
  files_error           text
  last_error            text
  cleared_at            timestamptz
  cleared_by            uuid references profiles(id) on delete set null
  restored_at           timestamptz
  created_at, updated_at
  indexes: (created_at desc), (cleared_at) where cleared_at is null; RLS enabled + forced; set_updated_at trigger

rfp_harvests            + project_id uuid references projects(id) on delete set null (index),
                        + create_claim_token text, create_claimed_at timestamptz
rfp_emails              + created_project_id uuid references projects(id) on delete set null (index)
                        status check gains 'create', 'created'
rfp_portal_invitations  + created_project_id (same); status check gains 'create', 'created'
                        (drop/re-add by column lookup, the 0124 pattern)
project_files           + rfp_harvest_id uuid references rfp_harvests(id) on delete set null
                        + rfp_sandbox_file_id uuid references rfp_ingest_files(id) on delete set null
                        unique index (project_id, rfp_sandbox_file_id) where rfp_sandbox_file_id is not null
llm_jobs.job_type       check gains 'rfp_create_files'
notify pgrst, 'reload schema'
```

Nothing in the migration touches `projects.number` values. The seed
statement reads them only.

---

## 7. Notifications, tasks, flags

- On creation: `notify_role(Role.EXECUTIVE, project_id, "rfp_create.created",
  "Project {number} {name} was created from an RFP invitation and is waiting in Go/No-Go")`
  and `notify_role(Role.ESTIMATING_ADMIN, project_id, "rfp_create.intake_needed",
  "Project {number} {name} was created from an RFP invitation; intake details needed: {missing}")`,
  both `mirror_email = True`; `notification_email.py` gains the two subjects.
  Bell metadata carries `{project_id, number, missing, source_kind}`.
- `app/services/project_intake.py`: `missing_intake_fields(project: dict, *, bid_time_unknown: bool = False) -> list[str]`
  (pure): `internal_bid_at`, `due_from_estimator_at`, `due_from_vendors_at`,
  each of the nine rubric keys that is null, plus `bid_time` when the
  created row's `bid_time_unknown` is true (the date-only case of 4.3; it
  clears when a person edits `actual_bid_at`). The list uses the project
  column names so the catalogs can label them.
- Dashboard: `_present` adds `rfp_intake_missing: list[str] | None` (null when
  the project was not created by this slice or nothing is missing). The
  dashboard row shows "Task for: Estimating Admin" + an "Intake details
  needed" chip while non-empty; the todos "Up next" list gets the same line.
  `dismiss_notifications(project_id, types=["rfp_create.intake_needed"])`
  fires from `update_project` when the list becomes empty.
- Page flags (computed by the page API, never stored except the sender
  marker and `last_error`): `missing_files` (`files_status = none`, or
  complete with `files_promoted = 0`), `no_gc` (no `project_gcs` row),
  `likely_gc`, `intake_incomplete` (the missing list), `sender_was_unauthorized`,
  `documents_skipped` (`files_skipped` non-empty), `files_failed`,
  `last_error` (a best-effort step of 4.5 that failed; the page shows a
  danger badge "Creation error" with the sentence as its hover text).
- `_present` drops `bid_time` from `rfp_intake_missing` for a role outside
  `ACTUAL_BID_VIEWER_ROLES`: the actual bid date is redacted for them, so
  nothing about it leaves the server.
- Go/No-Go gate (2026-09-27): no decision is taken on an RFP-created project
  while its intake is incomplete. `services/gono.ensure_intake_complete`
  (over `intake_missing_for`, which returns [] for a project without an
  `rfp_created_projects` row or while `RFP_INGESTION_ENABLED` is off) answers
  409 `Complete the intake details before deciding Go/No-Go: {phrase}`
  (`phrase` = `notification_email.intake_missing_phrase(missing)`) from
  `POST /projects/{id}/gono/decide`, from `apply_entry_action` for `go`,
  `no_go` and `score`, and from `POST /projects/{id}/advance` out of the
  intake task before the lane moves. `review` is never blocked (the creation
  step enters every project that way). `bid_time` is left out of the phrase
  for a role outside `ACTUAL_BID_VIEWER_ROLES`; when it was the only missing
  field the detail ends after "Go/No-Go". `GET /projects/{id}/gono` adds
  `intake_missing: list[str]` (same redaction; [] for any other project).
- Intake modal source facts: `ProjectOut.rfp_created` (`RfpCreatedSummary`)
  also carries `source_kind` (`rfp_email` | `rfp_portal`), `mailbox` (the
  receiving mailbox, `rfp_emails.primary_mailbox` via `rfp_email_id`, null
  for a portal source; one batched `in_()` read per page), `likely_gc_id`
  and `likely_gc_name`, so the modal can show where the invitation came from
  and the GC the creation step settled on.

---

## 8. API

`app/routers/rfp_emails.py` (existing router, `RFP_REVIEW_ROLES`):

- `POST /rfp-emails/{id}/create` -> `{project_id, number, linked, why,
  duplicate_of}`; `why` is null when the row's harvest already carried the
  project and `sibling` when the 4.6 guard joined it to the project another
  copy of the same message made (`duplicate_of` is that copy's email id).
  The recent-project half of the guard never links here: it refuses, naming
  the project. 409 with the `CreateRefused` sentence, 409
  `create_in_progress` while another worker holds the claim. Audit
  `rfp_email.create`, and `rfp_create.link` from the service on a link.
- `POST /rfp-emails/{id}/set-name {name}` -> the row; allowed on `done`
  rows without `created_project_id` that are not sibling followers; writes
  `extracted_project_name` and `extract_model = "human"`, CAS `done ->
  match` with `flag_reason = null`, `attempts = 0`; 409 otherwise, and 409
  for a name that normalizes to nothing (section 3's one rule). Audit
  `rfp_email.set_name` with old/new.
- `GET /rfp-emails/{id}` gains `created_project: {id, number, name} | null`
  and `create_available: bool` (status `done`, has a name, not sibling).
- List rows gain `created_project_id`; the `processed` tab includes `created`.

`app/routers/rfp_portal.py`: `POST /rfp-portal/invitations/{id}/create`
(same shape); detail gains `created_project`.

`app/routers/rfp_created.py` (new; Estimating Admin, Executive, IT Admin;
gated like `/rfp-emails` on `rfp_email_ingestion_enabled` OR
`rfp_ngem_active`, 404 otherwise):

- `GET /rfp-created?cleared=false|true|all&limit=100&before=<created_at>&before_id=<project_id>` ->
  `[{project: {id, number, name, current_stage, actual_bid_at, internal_bid_at, gcs: [{id, name}]},
     created: {created_at, created_by: {id, full_name} | null, automatic, source_kind, source: {id, subject | title, received_at | first_seen_at, invitation_method}, harvest_id},
     flags: {missing_files, no_gc, likely_gc: {id, name} | null, intake_incomplete: [...], sender_was_unauthorized, sender_display, sender_allowed_by: {id, full_name} | null, documents_skipped: n, files_status, files_promoted, files_error, files_failed, last_error},
     cleared_at, cleared_by}]`
  newest first (`created_at desc, project_id desc`), paged on the
  `(created_at, project_id)` pair: `before` is the last row's `created_at`,
  `before_id` its `project_id`, and the next page is every row strictly
  before that pair in the same order (PostgREST
  `or=(created_at.lt.X,and(created_at.eq.X,project_id.lt.Y))`), so rows
  created in the same instant never repeat or vanish. `before` alone pages
  on the timestamp only; `before_id` must be a uuid (400). One query per
  table, no N+1 over 100 rows.
- Filters on the list (added 2026-09-23, migration 0135), all optional and
  combinable with `cleared` and the paging pair:
  `created_from=<ISO instant>` (inclusive) and `created_to=<ISO instant>`
  (exclusive) bound `created_at`; each must parse as an ISO timestamp
  (naive reads as UTC) and `created_from` must be strictly before
  `created_to`, else 400. The page computes the instants: its presets
  (Today, Yesterday, This week, Last week, This month, Last month, This
  quarter, Last quarter, Year to date, Last year) and a custom range of
  days are Pacific-calendar windows (weeks start Sunday, calendar
  quarters, "Year to date" is Jan 1 through the end of today), each bound a
  Pacific midnight converted to UTC (`bdr_fe/lib/dateRanges.ts`), so the
  backend holds no time-zone logic. `q=<text>` (max 100 characters,
  trimmed, blank ignored) is a case-insensitive substring search over the
  project number and name, the linked GC names, the email subject or the
  portal invitation title, `sender_display` and `invitation_method`. It
  runs server side through the view `rfp_created_search`
  (`project_id, created_at, cleared_at, search_text`, search_text the
  lowercased `concat_ws(' ', ...)` of those fields; `security_invoker`,
  no grants to anon or authenticated): the page's project ids come from the
  view under the same cleared, window and paging filters and order, then
  the records are read by id and kept in that order. The text is literal:
  `%`, `_` and `\` are escaped; `*` (PostgREST's alias for `%`, which
  has no escape) is sent as the one-character wildcard `_`. The pattern is
  one filter param (`search_text=ilike.<pattern>`), never part of an
  `or=(...)` group, so commas and parentheses are safe.
- `POST /rfp-created/{project_id}/clear`, `POST /rfp-created/{project_id}/restore`
  (Estimating Admin, Executive, IT Admin; audit `rfp_created.clear|restore`).
- `POST /rfp-created/{project_id}/retry-files` -> re-enqueue when
  `files_status in (failed, none)` (or `running` under a stale claim, section
  5 step 7) and the harvest has entries; `pending` is written first, fenced,
  then the job; 409 when someone else retried first, when a job is already
  active (the prior status restored) or when there is nothing to promote.
- `GET /rfp-created/counts?created_from=&created_to=&q=` -> `{active, cleared}`:
  exact-count HEAD reads. With no params it is the sidebar badge, read off
  `rfp_created_projects` as before; the page passes its filter (the list's
  meanings and 400s; a search reads the view) for its "N projects" line.

`app/routers/projects.py`: `GET /projects/next-number` (section 2);
`ProjectOut` gains `rfp_created: {automatic, sender_was_unauthorized, sender_display, sender_allowed_by_name, files_status} | null`
and `rfp_intake_missing`. The project files list marks `from_rfp: bool`.

---

## 9. Frontend

- `components/NewProjectModal.tsx`: the number `Input` becomes a read-only
  preview from `GET /projects/next-number` ("Assigned on save: 26.9.7204",
  refreshed when the modal opens; a fetch failure shows "assigned on save")
  plus a "Budgetary (adds B)" `Checkbox`. `number` leaves `EMPTY_FIELDS`,
  the POST payload (`budgetary` joins it), the draft body and the
  Save-for-later gate (name only). The similar-projects and rebid flow is
  untouched.
- `components/EditProjectDetailsModal.tsx`: a read-only number line with
  the Budgetary toggle (PATCH `{budgetary}`); a 409 from a legacy number is
  shown as the sentence.
- `/rfp-emails` detail (`app/(app)/rfp-emails/page.tsx`): "Create project"
  primary button in the footer beside `RfpMatchFooterActions` when
  `create_available`; on success a Callout with the project link; when
  `created_project` is set the header shows a `Badge` "Created" linking to
  the project. "Set project name" on `RfpExtractedBlock` when the row is
  `done` with no name: inline input + Save, then the Are-you-sure modal
  (`ConfirmAdvanceModal` pattern) explaining the row goes back through
  matching. `RfpEmailStatus` gains `create`, `created`; `STATUS_TONE`
  entries; processed tab lists `created`.
- `components/RfpPortalInvitationModal.tsx`: the same "Create project"
  button and "Created" badge.
- New page `app/(app)/rfp-created/page.tsx` ("Created from RFP
  Ingestion"): table newest first (Number, Name, Bid due, Stage, Created
  (by/System, when), Source, Flags as badges), "Show cleared" toggle, row
  actions Clear / Restore / Retry documents, row click opens the project.
  A filter bar above the table: a created-date preset select (All time,
  the ten presets of section 8, Custom range with two inclusive day
  inputs), a search box debounced 300 ms, "Clear filters", and the count of
  matching rows ("14 projects", from the filtered `/counts`) with the
  window in Pacific days. The filter lives in the URL
  (`?range=this_week&q=...`, a custom range as
  `?range=custom&from=YYYY-MM-DD&to=YYYY-MM-DD`; `from`/`to` alone read as
  custom) so a filtered view can be shared; any change reloads from the
  first page and "Load older" pages under the same filter. A filter that
  matches nothing shows "No projects match these filters", distinct from
  the never-created empty state. Sidebar item in
  `BIDDING_NAV` with `roles: [estimating_admin, executive, it_admin]`,
  `featureFlag: ["rfp_email_ingest", "rfp_ngem"]` (either, the backend's own
  gate; `lib/rfpCreated.rfpCreatedServed`, shared by the page and the counts
  poll), badge = active count. The "Set project name" form shows for a
  `done` row that is `no_project_name` or has a blank name, never for a
  sibling follower.
- Project page header: "Created from RFP" badge (navy) and, when
  `sender_was_unauthorized`, a warn badge with the hover text "Sender was
  unauthorized, allowed by <name>". `FilesPanel` shows an info badge "From
  RFP" on promoted rows.
- Dashboard: "Intake details needed" chip + "Task for: Estimating Admin"
  when `rfp_intake_missing` is non-empty; the todos page line.
- Catalogs: `rfpCreated.*`, `newProjectModal.numberPreview`,
  `newProjectModal.budgetary`, `editProjectDetails.budgetary`,
  `rfpEmails.detail.actions.createProject|setName`, `projectPage.rfpCreated.*`,
  `dashboard.intakeNeeded`, in all six catalogs.

---

## 10. Configuration

| Setting | Env | Default | Meaning |
|---|---|---|---|
| rfp_create_auto_enabled | RFP_CREATE_AUTO_ENABLED | false | automatic creation at the `create` step; off = drain to `done`, button only |
| rfp_create_poll_seconds | RFP_CREATE_POLL_SECONDS | 30 | re-check interval while another worker holds a claim |
| rfp_create_claim_seconds | RFP_CREATE_CLAIM_SECONDS | 600 | a create claim older than this is stale |
| rfp_create_notes_max_chars | RFP_CREATE_NOTES_MAX_CHARS | 4000 | bid_notes cap |
| rfp_create_files_queue_priority | RFP_CREATE_FILES_QUEUE_PRIORITY | 160 | queue priority of the promotion job |
| rfp_create_duplicate_window_minutes | RFP_CREATE_DUPLICATE_WINDOW_MINUTES | 60 | create-time duplicate guard (4.6): a project made this recently with the same name for the same GC is joined, not duplicated; 0 disables that half |

The unique-violation retry cap when assigning a number is a module
constant, `project_numbers.NUMBER_MAX_TRIES` (20), not a setting.

All gated by `rfp_ingest_enabled` (the master switch). `.env.example` gains
the block. Release needs 0130 on staging then production plus
`RFP_CREATE_AUTO_ENABLED` in Railway, with explicit approval.

---

## 11. Security notes

- The promoted bytes are exactly what the sandbox verified: same object,
  sha256 re-checked against `rfp_ingest_files.sha256` after download; a
  mismatch skips the file ("changed since verification"), a missing digest
  skips it too ("no_digest"). A converted PDF is checked against the
  manifest's conversion digest. Both hazard sources (PDFium counters and
  the byte-marker scan of the bytes) must be present and zero; an OOXML original
  must pass the container scan (section 5) or lands as its converted PDF.
- The two `rfp_create.*` notification emails render their message without
  the linkify pass (`notification_email.linkify_for`): the project name in
  them came from an outside sender. Every other notification email is keyed
  by the source of each URL, not by type: that project name (and estimator
  note text, vendor names) can appear in any message, so only URLs on the
  app's own frontend host (`notification_email.link_hosts_for`,
  `email_branding.house_link_hosts` for the renderer's default) become
  anchors; any other URL renders as plain escaped text.
- `next_project_number()` is service-role only; the counter table has RLS
  forced and no policies.
- The page API redacts nothing new: numbers, names and flags are visible to
  the three roles; scores are not returned.
- `set-name` is a plain text field capped at 200 characters, trimmed,
  control characters stripped, audited with old and new.
- The manual `create` route requires `RFP_REVIEW_ROLES` (the review-queue
  roles); creation itself needs no write on `projects` from the client.

---

## 12. Tests

- `tests/test_project_numbers.py`: format/parse/format round trip, prefix in
  Pacific time across the year boundary (Dec 31 23:30 PT), wrap 9999 -> 0001,
  preview never advances, assign retries on unique violation and gives up
  at the cap, `with_budgetary` on legacy numbers raises.
- `tests/test_projects_router_numbers.py`: POST ignores a client `number`,
  assigns, concurrent inserts get distinct numbers (fake rpc with a
  counter), PATCH budgetary toggles, legacy 409, next-number route.
- `tests/test_rfp_create.py`: facts for Procore / email / portal rows,
  date-only handling, GC plan (all four kinds), claim + lost race
  compensation, harvest-mate linking, sibling refusal, preconditions,
  notifications, missing list, and the 4.6 duplicate guard (sibling already
  created in either direction, a recent project with the same name and GC,
  another GC, another name, outside the window, abandoned, window 0, a row
  with no resolved GC, the button path releasing its claim, the test-bench
  `linked` event).
- `tests/test_rfp_create_files.py`: promotion decision table (every skip
  reason), category mapping, idempotent retry, sha mismatch, doc/xls
  converted path, drawing bell once.
- `tests/test_rfp_created_router.py`: list shape/order/paging, clear/restore,
  role gate, 404 when the feature is off; the filters (creation window
  bounds and 400s, search over every field with wildcards and filter
  syntax as literal text, paging under a search, filtered counts, the
  no-param counts unchanged) and the 0135 view text.
- Existing suites updated: `_park_done` targets, `_finish_email` targets,
  `STATUS_*` tuples, 0124/0127/0129-style migration text assertions for the
  two status CHECKs.

---

## 13. Build record

**Built 2026-09-16, DEV ONLY, uncommitted.** Three parallel build agents
(numbering + `/projects`; creation service + pipeline + `/rfp-created`;
frontend), then a two-lens adversarial review (Review 1 below), then a
live smoke on the dev server. Backend suite 4523 passed (one pre-existing
env-caused failure, `test_match_stats_answers_the_services_tally`, reads
the local `.env`); ruff clean on every touched file; FE lint, tsc and
build green. Migration 0130 applied to the dev database only; the counter
seeded at 7118 (dev's own highest).

### Live run (2026-09-16, dev :5051 + :4500, E2E account, button path, flag off)

- `GET /projects/next-number` -> `26.9.7119`; `POST /projects` with a
  client-supplied junk number and `budgetary: true` -> `26.9.7119B`;
  PATCH `{budgetary: false}` -> `26.9.7119`; discarded.
- Cimarron-Memorial High School (Procore, 22 files): `26.9.7120` at
  `go_no_go` / Executive, name, actual bid date (Procore instant),
  address, bid sheet link, bid notes, invitation date, 12-item intake
  list; the email row `created`; bells `rfp_create.created` +
  `rfp_create.intake_needed`; promotion 19 promoted + 3 skipped
  `marker:/AA` (false positives from the sniff's substring count on
  `/AAPL:Keywords`, which led to the token-bounded byte scan of the
  promoted bytes in section 5); after "Retry documents" 22 of 22, no
  duplicate rows (the unique index), categories 16 specification / 1
  electrical_drawing / 5 other, 60.7 MB.
- UMC MLK Warehouse Remodel (`nonorganic`, EOC, 16 files): `26.9.7121`,
  GC "EOC Estimates" created from the sender with the contact attached
  and selected, marker "sender was unauthorized, allowed by <user>", the
  SharePoint folder as the bidding link, 16 of 16 promoted.
- Sewer Rehabilitation Group L (Procore, two emails sharing one harvest,
  102 files): `26.9.7122` from the first email; the second email flipped
  to `created` on the same project (mate linking); a second click on it
  answers 409 "A project was already created from this invitation."; 101
  promoted, 1 listed as `failed` (the sandbox's own verdict on that
  sheet).
- `set-name`: "Invitation to Bid" refused (409, normalizes to nothing);
  a real name sent the row `done -> match` with `extract_model = human`.
- `/rfp-created`: counts 3/0, clear -> 2/1, restore -> 3/0; list newest
  first with flags. Headless screenshots of the page, the New Bid modal
  (number preview + Budgetary), and both project pages (badges, intake
  callout, Go/No-Go review) rendered.
- Fixed from the run: the byte-marker check (above), a 0.6 score floor
  on "likely" GC candidates (a 0.16 candidate had named a test GC), retry
  allowed on a `complete` run that skipped documents, and the page table
  switched to a fixed layout so the flags and actions wrap.

Gotcha: `npm run build` while `next dev` is running overwrites `.next`
and leaves the dev server serving stale chunks (React never hydrates,
the HMR socket fails); restart the dev server after a build.

### Review 1 (2026-09-16)

Findings from the first code review, each fixed with a test:

- S1: promotion consults the byte markers beside the PDFium counters; after the live run this became a token-bounded scan of the promoted bytes themselves (`pdf_marker_keys`, `/URI` allowed) rather than the sniff's substring counts; a hazards column that is not a dict is `hazards_unknown`, never clean.
- S2: a docx/xlsx original passes `ooxml_container_verdict` (VBA, embeddings, binary parts, external links/relationships, DDE, macro-enabled, bounds) or lands as the converted PDF with `converted:<why>` on its note.
- S3: no verified digest is `no_digest` (never an unchecked promotion); a converted PDF is checked against `manifest.conversion.pdf_sha256` (`converted_sha_mismatch`), or noted `converted_unverified` when the manifest has none.
- S4: an existing GC found by `find_duplicate_company` is reused with no contact and recorded as `likely` (`gc_plan = created` only for rows this call inserted); compensation deletes the GC and contact it inserted.
- S5: the two `rfp_create.*` emails render without the linkify pass.
- S6: `rfp_created_projects.last_error` is returned in `flags` and shown as the "Creation error" badge.
- M1: one emptiness rule (`has_project_name`, normalized) across the match exit, the create step, the preconditions, the button and `set_project_name` (409 on a name that normalizes to nothing); the FE form keys off `no_project_name` or a blank name.
- M2: `reject_match` routes through `_park_target` (harvest or create) with the review fields, CAS from `review_match`.
- L1: the create step links a row whose harvest already has a project before the flag-off drain.
- L2: `files_status = pending` is CASed before the enqueue (creation and retry), restored on an enqueue failure.
- L3: a lost promotion claim raises the transient error; retry-files accepts a `running` record whose claim is older than the queue lease.
- L4: NGEM `ignore` accepts `create`.
- L5: `set_project_name` and the FE refuse a sibling follower, pointing at the leader.
- L6: the `rfp_created_projects` row is inserted right after the project, before the GC links and the state seed.
- L7: `set_project_name` stamps `extract_model = "human"`; `facts_for_email` then prefers the typed name over a harvest's.
- L8: the unauthorized marker needs `continued_by` or the `rfp_email.continue` audit row; the method alone does not count.
- L9: the page pages on the `(created_at, project_id)` pair (`before` + `before_id`).
- L10: the likely-GC domain lookup filters `gc_contacts` server-side with an escaped `ilike` and a 500-row cap.
- L11: the FE gate for the page, the counts poll and the nav item is `rfp_email_ingest OR rfp_ngem` (NavItem.featureFlag takes a list).
- L12: `_present` drops `bid_time` from `rfp_intake_missing` for roles outside `ACTUAL_BID_VIEWER_ROLES`.
