# RFP Ingestion: Test Mode and Monitor

Design record for the dev-only test bench that lets the IT Admin exercise the
whole RFP Ingestion feature end to end against real forwarded mail, watch every
step as it happens, and see into the projects it creates. It sits on top of
the email intake (`RFP_EMAIL_INGESTION.md`), matching (`RFP_MATCHING.md`),
harvest (`RFP_HARVEST.md`), project creation (`RFP_CREATE.md`) and the older
project email filer (`app/services/email_ingest.py`).

Status: design approved 2026-09-16 (user answers recorded in section 1);
backend BUILT 2026-09-16 (section 13 is the build record; where the build
differs from the design the section text below was updated to match the
code). Dev only: migration 0131 goes to the dev database only, and
`RFP_TESTING_ENABLED` stays false in Railway.

Naming, used everywhere: setting prefix `rfp_testing_` (env prefix
`RFP_TESTING_`), service `app/services/rfp_test.py`, router
`app/routers/rfp_testing.py` under `/rfp-testing`, tables `rfp_test_sessions`
and `rfp_test_events`, tag column `test_session_id` on the rows a session
produces, `GET /features` key `rfp_testing`, FE page `/rfp-testing`, FE
namespace `rfpTesting`.

No em dashes anywhere (project rule).

---

## 1. Decisions locked in (2026-09-16)

| Topic | Decision |
|---|---|
| Who sees it | Dev accounts (`profiles.is_dev`) while their role is `it_admin`. Anyone else gets 403 from every route and the sidebar item is hidden. |
| Master switch | `RFP_TESTING_ENABLED` (default false). While false every `/rfp-testing` route 404s with the bare "Not Found" body, like `/rfp-emails` while its switch is off. Activation additionally requires `RFP_INGESTION_ENABLED`. |
| Test mailbox | `RFP_TESTING_MAILBOX` (local .env: `symone@g3electrical.com`). Inbox folder only. |
| Accepted sender | `RFP_TESTING_SENDER` (local .env: `t.moorejr@g3electrical.com`). The user forwards real invitations from this mailbox to the test mailbox. Every other message in the test mailbox is ignored and listed on the page as ignored. |
| Which messages | Only messages whose `receivedDateTime` is at or after the session's `started_at`. Older mail in the test mailbox is never read. |
| Forwards | Unwrapped (user: "Unwrap the forward"). The pipeline judges the ORIGINAL sender, subject and date, so auth, classify, authorize, method (Procore, PipelineSuite, GC domain), extract, match and harvest all run as if the GC had sent it. Section 5. |
| Other mail while active | Paused (user: "Pause them all"). The four real RFP mailboxes, the NGEM portal scan and the older project email filer's own mailbox pull nothing while a session is active, and their pending rows wait. They resume where they left off on deactivate. Section 4. |
| Outbound mail while active | Redirected (user: "redirect to baseballtom33@gmail.com, but simplify the message to what it is saying. no html and specify which user it is for"). Every send from this server while a session is active goes to `RFP_TESTING_REDIRECT_TO` as plain text with a header naming the intended recipient. Section 6. |
| Monitor scope | RFP intake end to end, follow-up mail INTO test projects (the project email filer, watching the test mailbox during the session), and mail OUT of test projects (RFQ sends, nudges, proposal sends, notifications). |
| Poll cadence | `RFP_TESTING_POLL_SECONDS` (default 15) replaces the 120 second intervals of both pollers while a session is active. |
| Project creation | Follows `RFP_CREATE_AUTO_ENABLED` (button-first) unless the session was activated with `auto_create=true`, in which case test rows create automatically at the create step. |
| Session data | Everything a session produces is tagged with its id. "Clean up" on an ENDED session deletes those rows (section 9). The session row and its events are kept as the record. |
| Migration | `0131_rfp_testing.sql`, dev only, never prod without approval (`never-touch-prod-without-approval`). |

---

## 2. Gates

Three gates, all fail closed:

1. `RFP_TESTING_ENABLED` false: router 404s ("Not Found"), `GET /features`
   reports `rfp_testing: false`, the pollers never consult the session table,
   the outbound redirect is inert, the sidebar item is hidden.
2. Caller must be `is_dev` AND `role == it_admin`; else 403
   `rfp_testing_forbidden` (the code is the detail and the `X-Error-Code`
   header). Implemented as `require_dev_it_admin` in the router on top of
   `get_current_user` directly (not `require_dev`), so a non-dev and a dev in
   another role get the same code and the page renders one "not available"
   block.
3. Activation preflight (section 3.2) must pass.

`GET /features` gains `rfp_testing = settings.rfp_testing_enabled and
settings.rfp_ingest_enabled`. The FE hides the page unless that flag is true
AND the profile is dev AND the role is `it_admin`.

Settings (`app/core/config.py`, `Settings`):

```
rfp_testing_enabled: bool = False
rfp_testing_mailbox: str = ""            # RFP_TESTING_MAILBOX
rfp_testing_sender: str = ""             # RFP_TESTING_SENDER
rfp_testing_redirect_to: str = ""        # RFP_TESTING_REDIRECT_TO
rfp_testing_poll_seconds: int = 15       # RFP_TESTING_POLL_SECONDS (min 5)
rfp_testing_unwrap_scan_chars: int = 4000  # RFP_TESTING_UNWRAP_SCAN_CHARS (min 500), section 5.1
```

Boot validation (`Settings._validate_rfp_testing`): when
`rfp_testing_enabled` is true, mailbox, sender and redirect_to must be
non-empty and well formed addresses, and the mailbox must not be one of
`rfp_email_ingestion_inboxes` or equal to `email_ingest_mailbox` (the test
mailbox is dedicated). The poll interval and the scan window are checked
whatever the switch says. The prod config guard (`_validate_production`)
refuses to boot with `rfp_testing_enabled` true when `ENVIRONMENT` is
production. The check fails closed: only development, dev, local or test
(case-insensitive) count as dev; any other value, staging included, gets the
production guards.

Boot (`app/main.py`): both pollers also start when the bench is on, so a
dev box with no real RFP mailbox listed, or the filer switched off, still
runs them at the test mailbox while a session is active.

Local `.env` additions (the build sets them):

```
RFP_TESTING_ENABLED=true
RFP_TESTING_MAILBOX=symone@g3electrical.com
RFP_TESTING_SENDER=t.moorejr@g3electrical.com
RFP_TESTING_REDIRECT_TO=baseballtom33@gmail.com
```

---

## 3. Sessions

### 3.1 Table `rfp_test_sessions`

```
id                 uuid pk default gen_random_uuid()
status             text not null check (status in ('active', 'ended'))
name               text                         -- optional label typed at activation
started_by         uuid references profiles(id) on delete set null
started_at         timestamptz not null default now()
ended_by           uuid references profiles(id) on delete set null
ended_at           timestamptz
mailbox            text not null                -- snapshot of the settings at activation
sender             text not null
redirect_to        text not null
auto_create        boolean not null default false
intake_last_tick_at   timestamptz              -- heartbeat: RFP intake poller
filer_last_tick_at    timestamptz              -- heartbeat: project email filer
cleanup_started_at    timestamptz
cleanup_finished_at   timestamptz
cleanup_report        jsonb                    -- what was deleted / kept (section 9)
created_at         timestamptz not null default now()
```

Partial unique index: `create unique index rfp_test_sessions_one_active on
rfp_test_sessions ((status)) where status = 'active'` so two activations can
never race into two active sessions. RLS enabled and forced like every other
table (service role only).

### 3.2 Activate: `POST /rfp-testing/sessions`

Body `{ name?: string, auto_create?: boolean }`. Steps, in order:

1. 409 `rfp_testing_already_active` if an active session exists.
2. Preflight (`rfp_test.preflight`), each failure a 422 with a code the
   page can show verbatim, cheap checks first so a misconfiguration never
   spends a Graph round trip:
   - `rfp_testing_ingest_off`: `RFP_INGESTION_ENABLED` is false.
   - `rfp_testing_graph_off`: `ms_client_id` is empty.
   - `rfp_testing_redirect_invalid`: redirect address malformed.
   - `rfp_testing_mailbox_unreachable`: `GET /users/{mailbox}/mailFolders/inbox`
     via `graph_request` did not return 200 (the detail is
     `rfp_testing_mailbox_unreachable: <status>`; 404 means the mailbox does
     not exist in the tenant, 403 means the app registration's access policy
     does not cover it; a transport error carries the exception name).
3. Insert the session (snapshotting mailbox / sender / redirect_to from the
   settings). The unique index turns a race into the 409.
4. Reset the test mailbox's delta link (`graph_sync_state` ids
   `rfp-mail:{mailbox}:inbox` and the filer's `pm-mail:{mailbox}:inbox`) by deleting those rows, so the
   first tick does an initial pull. Messages older than `started_at` are
   dropped in `_insert_from_delta` (section 4.1), so the lookback window is
   irrelevant.
5. Record event `session.activated`.
6. Return the session (section 7 shape).

### 3.3 Deactivate: `POST /rfp-testing/sessions/{id}/end`

Sets `status='ended'`, `ended_at`, `ended_by`; records `session.ended`. Rows
tagged with an ended session are frozen: neither poller sweeps them again
(section 4). The paused pollers resume on their next tick. Idempotent (a
second call on an ended session returns it unchanged).

### 3.4 Reading the active session cheaply

`rfp_test.active_session(sb) -> dict | None` reads the single active row.
The pollers call it once per tick (never per row); `send_mail` / `send_draft`
call it once per send. When `rfp_testing_enabled` is false it returns None
without a query. No process-level cache beyond that: the two production
workers must agree, and a tick is the right granularity.

---

## 4. What test mode changes in each poller

The rule everywhere: normal mode touches only rows with `test_session_id IS
NULL`; test mode touches only rows with `test_session_id = <active id>`.
Ended sessions' rows are never touched again (until cleanup deletes them).

### 4.1 RFP email intake (`rfp_email_ingest.py`)

`poll_once`:
- `session = rfp_test.active_session(sb)`.
- Mailboxes: `[session.mailbox]` when a session is active, else the normal
  list. `watched` passed to `_sync_mailbox` is the normal list plus the test
  mailbox in both modes (so a message from the test mailbox itself is still
  internal).
- `_sync_mailbox(sb, mailbox, watched, session=session)`: in test mode, a
  message is inserted only when ALL hold: From address equals
  `session.sender` (case-insensitive), `receivedDateTime >= started_at`, not a
  tombstone. Everything else in the test mailbox is skipped AND recorded as
  event `intake.ignored` with the From, subject, received time and reason
  (`not_test_sender`, `before_session`) so the page shows what was dropped.
  Rows inserted in test mode carry `test_session_id = session.id`. The
  internal-sender skip is bypassed for the test sender in test mode ONLY
  (`should_skip_sender` gains a keyword `allow_addresses`, an explicit set).
  The vendor and blocked-domain skips and the RFQ-thread skip still apply to
  the raw From (they cannot match `t.moorejr`, so they are inert here) and
  are re-applied to the unwrapped sender in the fetch step (section 5.3).
- Sweep: `_sweep` filters `test_session_id` per the rule above (`is_("test_
  session_id", "null")` in normal mode, `eq(...)` in test mode).
- Heartbeat: after the sync, `update rfp_test_sessions set intake_last_tick_at
  = now()`.
- The review-queue notification (`_notify_review_queue`) still runs; its
  emails are redirected like every other send.

`polling_loop`: `poll_once` returns whether the tick ran in test mode and
the loop sleeps `rfp_test.poll_seconds(...)`: `rfp_testing_poll_seconds`
after a test-mode tick, the normal interval otherwise (read every
iteration).

Listing details as built: in test mode the initial pull (and a reset after
a DeltaExpired) looks back one day rather than the normal window, since the
session filter drops everything older than `started_at` anyway; a message
the raw From would have dropped by the vendor, blocked-domain or RFQ-thread
rule is recorded as `intake.ignored` too, with that rule's reason
(`vendor`, `listing_skip`, `rfq_thread`), because `_insert_from_delta` now
returns the skip reason; `intake.listed` is recorded only on a fresh insert
(a re-pulled duplicate adds its sighting silently).

### 4.2 Project email filer (`email_ingest.py`)

`poll_once`:
- In test mode the mailbox is `session.mailbox`, folder list is `["inbox"]`
  only (Sent Items of the test mailbox is nothing we sent), lease key follows
  the mailbox as it does today.
- `_insert_from_delta`: in test mode insert only when From equals
  `session.sender` and `receivedDateTime >= started_at`; tag
  `ingested_emails.test_session_id`. Skips are recorded as `filer.ignored`
  events (same reasons as 4.1).
- `process_pending`: same `test_session_id` filter rule.
- Heartbeat `filer_last_tick_at`.
- Interval as in 4.1 (`poll_once` returns the mode, the loop reads it).
- The lease key follows the test mailbox (`pm-mail:{mailbox}:lease`), so the
  filer's own mailbox lease is untouched and resumes as it was.

The same forward reaches both the intake and the filer (both watch the test
mailbox). That is expected: in production the two watch different mailboxes,
so the filer seeing an invitation and filing it as Unknown is a harmless test
artifact, and it is what lets a forwarded addendum land in the project the
intake created. No cross-skip is built. The filer does NOT unwrap forwards:
its R1 (conversation map) and R2/R3 (project number and LLM match over
subject and body) work on the forwarded text as is, which is how a
colleague's forward reaches it in production too.

### 4.3 NGEM portal scan (`rfp_portal_ingest.py`)

`poll_once` (the tick entry, run by `_tick` from `polling_loop`) returns
immediately while a session is active, right after the `rfp_ngem_active`
check and before the slot claims, recording nothing (it is a scheduled
scan; the page shows "paused" from the session state, not from an event).

### 4.4 Harvest and create (queue jobs and steps)

Harvest runs inside the llm_jobs worker as `rfp_harvest` jobs. Jobs enqueued
for test rows carry the session id in the job payload (`test_session_id`,
`rfp_harvest.enqueue(..., test_session_id=)`, passed by `step` only for a
tagged row so the normal call is unchanged) and a NEW `rfp_harvests` row
gets `test_session_id` from the email that started it
(`_find_or_create_harvest(..., test_session_id=)`). A real, untagged
harvest row a test row happens to reuse (same platform object) stays
untagged in the table; the tag rides on the in-memory dict for that job so
its run and file events are still captured. The sandbox run a harvest
creates carries the tag (`rfp_ingest.create_harvest_run(...,
test_session_id=)`). Real rows' harvest jobs that were already queued
before activation still run (they were in flight); that is acceptable and
stated on the page's paused banner ("harvests already queued keep
running").

`_step_create(sb, row, auto_create=...)` on a test row: automatic when the
session's `auto_create` (the sweep passes it from the active session) OR
`RFP_CREATE_AUTO_ENABLED`; otherwise the existing button-first behaviour
(the row parks at `done`, event `create.parked`) and the page links to the
RFP email's detail where the Create button lives. `rfp_create._create`
stamps `projects.test_session_id`, `rfp_created_projects.test_session_id`
and, for a GC or contact it inserts, `general_contractors.test_session_id`
/ `gc_contacts.test_session_id` (the GC table is `general_contractors`; a
reused existing GC is never tagged).

### 4.5 Everything else

Due reminders, the daily digest, RFQ inbox polling, LLM health and the rest
keep running unchanged. Their sends are redirected while a session is active
(section 6). Nothing else is paused.

---

## 5. Forward unwrapping (test rows only)

Applied in `_step_received` after the fetch, only when `row.test_session_id`
is set. `rfp_test.unwrap_forward(row, full_message, attachments_meta,
mailbox, graph_message_id) -> Unwrapped | None`.

### 5.1 Inline forward (Outlook default, the primary path)

Outlook and Gmail forwards carry the original message as text inside the
body, with the original attachments re-attached as ordinary file attachments
(so the harvester's Graph attachment path works unchanged). Recognised header
blocks, matched case-insensitively at the start of a line, in this order:

```
From: Name <addr@x>            (Outlook desktop and web; "Sent:" and "Subject:" follow)
-----Original Message-----      (older Outlook; "From:" follows)
---------- Forwarded message ---------   (Gmail; "From:", "Date:", "Subject:", "To:" follow)
Begin forwarded message:        (Apple Mail; "From:", "Subject:", "Date:", "To:" follow)
```

From the block take `from_name`, `from_address` (the angle-bracket address,
a bare address, or Outlook's `Name [mailto:addr]`), `subject` (with leading
`FW:`, `Fwd:`, `FW :`, `TR:` prefixes stripped, repeated; the forward's own
subject, stripped the same way, when the block has none), and the date
from `Sent:` or `Date:`: `email.utils.parsedate_to_datetime` first (a naive
result is read as company Pacific time), then the wall-clock forms the
three clients write ("Tuesday, September 15, 2026 9:12 AM", "Tue, Sep 15,
2026 at 9:12 AM", "September 15, 2026 at 9:12:34 AM PDT") as Pacific; when
nothing parses the forward's `receivedDateTime` is kept. A block counts
only when it carries `From:` and at least one of `Subject:` / `Sent:` /
`Date:`, so a "From:" in prose never matches. The effective body is the
text AFTER the header block (the forward's own note above the block, if
any, is kept in `forward_meta`). The block must appear within the first
`RFP_TESTING_UNWRAP_SCAN_CHARS` (default 4000) characters of the body; a
block further down is quoted history, not the forward. Pure helpers:
`rfp_test.parse_inline_forward`, `parse_address`, `parse_forward_date`,
`strip_forward_prefixes`.

### 5.2 Forward as attachment (.eml / itemAttachment)

When the forward's listing has an `itemAttachment` (or a file attachment
named `*.eml` / content type `message/rfc822`), fetch its MIME with
`GET /users/{mailbox}/messages/{id}/attachments/{attId}/$value`
(`rfp_test.fetch_eml_bytes` over `graph_stream`, size-capped at
`inbound_attachment_max_bytes`) and parse it with the standard `email`
package (`rfp_test.parse_eml`): effective From / Subject / Date and the text
body (the plain part, else the HTML part through `rfp_harvest.html_to_text`).
The attachment listing the fetch step needs the ids for, so
`_list_attachment_meta(..., with_ids=True)` is used for test rows only and
the ids are stripped before the store. The MIME's own attachments become
harvestable through a new locator kind in `rfp_email_harvest`:
`eml:{mailbox}|{message_id}|{graph_attachment_id}|{part_index}`
(`eml_locator`), resolved by `EmailFileSession.download` by re-fetching the
MIME and writing that one part's payload (`rfp_test.eml_part_payload`;
never more than one part held past the parse). `_list_attachments` returns
the parts recorded in `forward_meta.eml_parts` for an unwrapped `.eml` row
(each `id` the locator) instead of the forward's listing, so the harvest
body needs no second Graph listing. The `.eml` itself is not harvested as a
file. An attached message that cannot be read falls through to the inline
block (5.1).

### 5.3 What the unwrap writes

On the `rfp_emails` row (all inside the same CAS that stores the body):

- `from_address`, `from_name`, `subject`, `received_at`, `body_text`,
  `body_preview` = the effective values.
- `forward_meta` jsonb = `{ "unwrap": "inline" | "eml" | "none", "raw_from_address",
  "raw_from_name", "raw_subject", "raw_received_at", "forwarder_note",
  "eml_attachment_id"?, "eml_parts"?: [{index, name, content_type, size}] }`.
  `"none"` means no block was found: the row proceeds with `t.moorejr` as the
  sender and the page shows a warning chip ("no forwarded header found").
- `attachments_meta` unchanged for inline; the `.eml` parts for 5.2.

After unwrapping, the effective sender is re-checked with the listing-time
rules (`_test_listing_skip_reason`: a watched mailbox, an internal domain, a
blocked platform domain, a vendor address or domain). A hit does NOT drop
the row silently: the row goes terminal `failed` with `flag_reason =
'test_listing_skip'`, `decided_at_step = 'fetch'` and `last_error = "In
production this message would never get a row: <reason>"` (the reason
names the rule, e.g. "g3electrical.com is an internal domain", "x@y is a
vendor contact (or a vendor domain)"), the unwrapped values are still
written so the page shows the effective sender, and events `fetched` (warn
when no block was found) and `listing_skip` (warn) are recorded. This is
the honest answer to "what would production do with this email". A forward
with no recognised block keeps `t.moorejr` as the sender, which is internal,
so it ends here too.

Auth (`_step_auth`) judges the FORWARD's `Authentication-Results` (the only
header we have; the original's headers are gone in an inline forward). The
forward is intra-tenant mail from the accepted sender, and Exchange Online
stamps no SPF / DKIM / DMARC pass on its own users (often no usable tokens
at all), so for a TEST row the verdict is the tenant's trust in that
sender: when the ordinary policy reads anything but pass, the row still
passes to `keywords`, and the `auth` event records the tokens seen plus the
note `"... the policy read fail on the tenant's own user, so the row passes
on the tenant's trust in the accepted sender"`. When the policy passes on
its own the note is the plain `"judged on the forward's headers (original
headers are not available)"`. Real rows are judged exactly as before.

---

## 6. Outbound redirect (while a session is active)

`rfp_test.redirect(message: dict, *, sb, session, context) -> dict` rewrites a
Graph message dict in place and is applied at the two choke points:

- `graph_email.send_mail`: before the POST, on the assembled `message`.
- `graph_email.send_draft(message_id, *, sender=None, project_id=None,
  rfq_id=None)`: before the send, GET the draft (`subject, body,
  toRecipients, ccRecipients, bccRecipients` with `$expand=attachments(id,
  name, size, isInline, contentType)`), build the rewritten fields, PATCH
  them onto the draft, DELETE its inline attachments (the logo; the body is
  text now), then send it. The draft's id and conversationId stay valid, so
  `rfq_sends` threading is unchanged. `create_reply_all_draft` sends go
  through `send_draft` too. The RFQ send and the RFQ nudge pass
  `project_id` / `rfq_id` so the event can name them; `send_draft` touches
  Supabase only while the switch is on.

Rewrite:

- `toRecipients = [redirect_to]`, `ccRecipients = []`, `bccRecipients = []`.
- `subject = "[TEST for {who}] {original subject}"` where `who` is the first
  intended To recipient's name (the resolved label without its role / kind
  suffix, e.g. `Jane Doe` or `Meridian Builders: Sam Lee`; the bare address
  when unknown; the first CC when there is no To) and `+N` when there are
  more (`[TEST for Jane Doe +2] ...`).
- `body = {"contentType": "Text", "content": header + "\n" + plain}` where
  `plain` is the original body converted to text (`rfp_harvest.html_to_text`
  is the existing converter; keep line breaks for `<br>`, `</p>`, `</div>`,
  `</tr>`, `</li>`, decode entities, collapse three or more blank lines to
  two, strip the tracking / signature image alt text) and `header` is:

```
BDR TEST MODE  (session "Wave 1", started Sep 16, 2026 3:12 PM PT)
This email was going to:
  To:  Jane Doe, Executive <jane@g3electrical.com>
  To:  Meridian Builders: Sam Lee, GC contact <sam@meridianbuilders.com>
  CC:  Tom Moore, IT Admin <t.moorejr@g3electrical.com>
From mailbox: bids@g3electrical.com
Project: 26.9.7124 Cimarron Elementary (when known)
Attachments kept: Plans.pdf (12.1 MB), Specs.pdf (3.4 MB)
--------------------------------------------------------------
```

Recipient display: look the address up in `profiles` (full name + role
label), then `gc_contacts` (GC name + contact name, "GC contact"), then
`vendor_contacts` (vendor name + contact name, "vendor contact"), else the
bare address with "(unknown)". One query per table per send, addresses
batched with `in_`.

Attachments are kept (they are what the user wants to check on RFQ and
proposal sends). Inline images (the logo) are dropped since the body is text.

Every redirected send records event `mail_out.redirected` (after the send;
`status` sent or failed, level warn on a failure) with: intended `to` /
`cc` / `bcc` (address and resolved display), `subject`, the redirected
subject and address, `project_id` (and the project label), `rfq_id`,
attachment names and sizes, the inline names dropped, the sender mailbox,
the plain preview (first 2000 characters) and the `email_log` id when there
is one (`send_mail`; a draft send's ledger row is written by its caller).
`email_log` keeps `to_addrs` = the INTENDED To line (proposal_send's crash
recovery compares it to `proposal_sends.gc_email`) and, on `send_mail`
rows made during a session, gains `test_session_id` and `redirected_to` so
the record is honest (the two columns are only written while a session is
active, so a deployment without migration 0131 never names them).

If the redirect address is empty (misconfiguration slipped past boot) the
send is refused (`rfp_test.RedirectRefused`, event `mail_out.refused` at
level error, the `email_log` row marked failed) rather than delivered to
the real recipients: fail closed. `send_draft` refuses before the PATCH.

---

## 7. Event ledger

### 7.1 Table `rfp_test_events`

```
id              bigint generated always as identity primary key
session_id      uuid not null references rfp_test_sessions(id) on delete cascade
at              timestamptz not null default now()
source          text not null   -- 'session' | 'intake' | 'filer' | 'harvest' | 'create' | 'project' | 'mail_out' | 'human'
kind            text not null   -- see 7.2
level           text not null default 'info' check (level in ('info', 'warn', 'error'))
title           text not null   -- one line, human readable
rfp_email_id    uuid references rfp_emails(id) on delete set null
ingested_email_id uuid references ingested_emails(id) on delete set null
harvest_id      uuid references rfp_harvests(id) on delete set null
project_id      uuid references projects(id) on delete set null
detail          jsonb not null default '{}'::jsonb
index (session_id, id)
```

`rfp_test.record(sb, *, session_id, source, kind, title, level='info',
detail=None, rfp_email_id=None, ingested_email_id=None, harvest_id=None,
project_id=None)`: one insert, wrapped so it NEVER raises into the pipeline
(log at warning and continue). `detail` is capped at 64 KB (long strings
truncated with a marker) so a prompt never bloats a row.

A helper `rfp_test.session_for_email(row) -> str | None` returns
`row.get("test_session_id")`; every capture point is a one-liner guarded by
it, so the normal path costs one dict lookup.

### 7.2 Capture points (kinds)

Intake (`source='intake'`), keyed by `rfp_email_id`:

| kind | where | detail |
|---|---|---|
| `listed` | `_insert_from_delta` (test insert) | raw from, subject, received_at, has_attachments, graph id, sighting |
| `ignored` | `_sync_mailbox` (test skip) | from, subject, received_at, reason |
| `fetched` | `_step_received` | unwrap method, raw vs effective from / subject / date, forwarder note, attachments (name, type, size, inline), auth tokens |
| `listing_skip` | `_step_received` (5.3 re-check hit) | reason, effective sender, level warn |
| `auth` | `_step_auth` | spf / dkim / dmarc / compauth, verdict, next status, note about forward headers |
| `keywords` | `_step_keywords` | hits (word, where), next status |
| `classify` | `_step_classify` / `_finish_classify` | model, prompt version, system prompt, user messages (capped), raw result, answer, confidence, reasoning, threshold, next status; on failure the error kind and attempt count (level warn) |
| `authorize` | `_step_authorize` | rule source (locked / gc_domain / user / none), rule id and pattern, next status |
| `method` | `_step_method` | method, why (rule vs source) |
| `extract` | `_step_extract` | prompt, raw result, extracted fields, next status |
| `match` | `_step_match` | candidates (project number, name, score, reason), LLM verdicts, decision (new / merged / duplicate / review_match), target project |
| `waiting` | `_llm_gate` / `_wait_for_model` | feature, scope, why (level warn) |
| `retry` | `_retry_or_fail` | step, attempt, next_attempt_at, error (level warn) |
| `terminal` | `_terminal` | status, flag_reason, decided_at_step, last_error (level warn for failed) |

Harvest (`source='harvest'`, `harvest_id` and `rfp_email_id`): `started`
(harvester kind, method, platform reference, credentials present yes/no,
never the values, whether an earlier complete harvest is reusable), `file`
per downloaded manifest row (name, source: attachment / zip / link /
platform kind, provider, size, download status, error, sandbox file id;
the sandbox verdict and hazards land later on the run's file row and are
read from there, not captured again), `link` per share link (host,
provider, kind, label, outcome, error, file count, bytes), `finished`
(counts, the manifest's names and statuses, also on a reuse), `parked`
(claim lost, logins locked or unavailable: the row waits), `retry`
(transient trouble: back to the queue's ladder), `failed` (permanent).

Create (`source='create'`, `project_id` and `rfp_email_id`): `created`
(number, name, actual bid date, invitation date, address, bidding URL, GC
plan and what was inserted or reused, the harvest's file manifest and
whether a promotion job was queued, unauthorized marker, missing intake
fields, notifications sent to which roles, automatic or by whom), `failed`
(error; level warn for a refusal such as "already created", error
otherwise; from the sweep and the button alike), `name_required` (no
project name: set one on the email), `parked` (automatic creation off:
press Create project on the email).

Filer (`source='filer'`, `ingested_email_id`): `listed`, `ignored`,
`fetched` (attachments), `r1` (conversation map hit or miss), `r2` (number
found or not), `r3` (candidates, LLM verdict, confidence, threshold, model;
or "skipped: no model configured"), `assigned` (project, matched_by,
confidence, round), `unknown` (with the below-threshold suggestion).

Human (`source='human'`, `rfp_email_id` or `ingested_email_id`, and the
project when the action names one): every `/rfp-emails` action on a test
row, as kinds `review`, `continue`, `dismiss`, `set_method`, `merge`,
`duplicate`, `unmerge`, `acknowledge`, `reject_match`, `set_gc`, `reopen`,
`set_name`, `create_project`, and every filer manual assign or unassign on a
test row (`manual_assigned`, `unassigned`). Detail: action, actor, request
fields, the status the row was at. Captured in the service functions those
routes call (one line each, after the write that won its race), not in the
router.

Mail out (`source='mail_out'`): `redirected` (section 6), `refused` (empty
redirect address, level error).

Session (`source='session'`): `activated`, `ended`, `auto_create` (the
PATCH), `cleanup_started`, `cleanup_finished` (level warn when the report
has errors).

Intake additions beyond the table: `retry` carries `error_kind` for an LLM
failure; `waiting` carries `feature` and `scope`; `extract` also records a
sibling follower ("followed an earlier copy"); `terminal` is recorded by
`_terminal` itself for every terminal status (level warn for `failed`) and
carries `step` = `decided_at_step`; `create.parked` / `name_required` are
recorded by the create step before the `done` write.

Project (`source='project'`) is NOT captured by events: the page reads
`stage_events`, `notification_log` and `email_log` for the session's projects
live (section 8.3). Only capture what those tables do not already hold.

---

## 8. API: `/rfp-testing`

All routes: 404 while the switch is off, then `require_dev_it_admin`. JSON
shapes below are the contract the FE is built against.

```
GET  /rfp-testing/state?session_id=&after=<event id>
     -> {
          enabled: true,
          config: { mailbox, sender, redirect_to, poll_seconds, auto_create_env },
          active: Session | null,
          selected: Session | null,          # the session_id asked for, else active, else newest
          sessions: SessionSummary[],        # newest first, max 50
          paused: { rfp_mailboxes: string[], ngem: boolean, filer_mailbox: string | null,
                    note: "harvests already queued keep running" },
          heartbeat: { intake_last_tick_at, filer_last_tick_at, now },
          counts: { emails, ignored, projects, filed, mail_out, events },
          events: Event[]                    # for `selected`, id > after, ascending, max 500
        }

POST /rfp-testing/sessions            { name?, auto_create? }         -> Session      (section 3.2 errors)
POST /rfp-testing/sessions/{id}/end                                   -> Session
PATCH /rfp-testing/sessions/{id}      { auto_create }                 -> Session      (active only)
POST /rfp-testing/sessions/{id}/cleanup                               -> { report }   (ended only, section 9)

GET  /rfp-testing/sessions/{id}/emails
     -> { rows: EmailRow[] }
        EmailRow = { id, status, flag_reason, decided_at_step, from_address, from_name, subject,
                     received_at, forward_meta, method, llm_answer, llm_confidence,
                     extracted: { project_name, gc_name, bid_due, platform_reference },
                     match: { decision, project_id, project_number },
                     harvest_id, harvest_status, created_project_id, created_project_number,
                     steps: StepChip[],       # ordered: received, auth, keywords, classify, authorize,
                                              # method, extract, match, harvest, create; each
                                              # { step, state: 'done' | 'current' | 'waiting' | 'skipped' | 'failed' | 'pending',
                                              #   event_id?: number }
                     last_error, attempts, next_attempt_at, updated_at }

GET  /rfp-testing/sessions/{id}/ignored     -> { rows: [{ at, source: 'intake' | 'filer', from, subject, received_at, reason }] }

GET  /rfp-testing/sessions/{id}/filed
     -> { rows: [{ id, status, from_address, subject, received_at, project_id, project_number,
                   match_method, match_confidence, match_reasoning, last_error, updated_at }] }

GET  /rfp-testing/sessions/{id}/mail-out
     -> { rows: Event[] }                    # the mail_out events, newest first

GET  /rfp-testing/sessions/{id}/projects
     -> { rows: [{ id, number, name, current_stage, created_at, gc_names: string[],
                   files: number, filed_emails: number, mail_out: number, flags: string[] }] }

GET  /rfp-testing/projects/{project_id}
     -> {
          project: { id, number, name, current_stage, current_owner_role, internal_bid_at,
                     actual_bid_at, created_at, created_by, abandoned_at, test_session_id },
          category_state: { ...workflow.load_category_state },
          gcs: [{ gc_id, name, contacts: [{ name, email }], needs_by, sent_at }],
          files: [{ id, name, category, size, created_at, source }],
          filed_emails: [{ id, from_address, subject, received_at, match_method }],
          stage_events: [{ at, actor, from, to, category, note }],   # newest first
          notifications: [{ at, type, recipients, subject }],         # notification_log
          mail_out: Event[],                                           # for this project
          rfp_created: { flags, files_status, files_promoted, files_skipped, cleared_at },
          source_email: { id, subject, from_address, method } | null
        }
```

`Session = { id, status, name, started_by: { id, name } | null, started_at,
ended_at, mailbox, sender, redirect_to, auto_create, cleanup_finished_at,
cleanup_report }` (`SessionSummary` is the same shape).
`Event = { id, at, source, kind, level, title, rfp_email_id, ingested_email_id,
harvest_id, project_id, detail }`.

Shapes as built, where they add to the above: `paused.ngem` is whether the
NGEM slice is switched on (`RFP_INGESTION_ENABLED` and `RFP_NGEM_ENABLED`);
`heartbeat` reads the active session, else the selected one; the `filed`
rows' `match_reasoning` is a sentence built from `matched_by`,
`pipeline_round` and the confidence ("R3: the model matched the subject
(confidence 0.91)"); the `projects` rows' `flags` are
`unauthorized_sender`, `bid_time_unknown`, `gc_likely` / `gc_none`,
`files_failed` / `files_none`, `error`; the project detail's `project` also
carries `created_by_name`, its `rfp_created` block also carries `automatic`
and `last_error` (null when the project has no record), its
`notifications` rows come from `notification_log.build` (each with `title`
and `channels` too, recipients as "Name (role)" strings), its `files` get
`source` = `rfp_harvest` / `upload` / `system`, and only a project tagged
with a session answers (404 otherwise). `EmailRow.steps` is
`rfp_test.step_chips`: the state comes from the row's status (a pending
row parked behind `next_attempt_at` is `waiting`, a human lane is
`waiting` on the deciding chip, the refusals are `failed` at the deciding
step, merged / duplicate skip harvest and create, a row that never
harvested shows harvest `skipped`) and `event_id` is the latest event about
that step. Ids on the path go through a uuid guard (404, never a 500).

Rate limits: the state route is polled every 3 seconds by one person, so
every route rides the generous catch-all budget
(`RateLimitScope.DEFAULT`, `default_rate_limit_per_min`, 240 per minute)
rather than the 120 per minute dev-page buckets.

---

## 9. Cleanup (`POST /rfp-testing/sessions/{id}/cleanup`)

Ended sessions only (409 `rfp_testing_session_active` otherwise). Runs in a
thread (sync SDK, never inside `async def`), records `cleanup_started`, then
deletes in dependency order and reports counts:

1. Projects with `projects.test_session_id = id`: first the `email_log`
   rows whose `project_id` is one of them (that FK only sets null, and the
   RFQ / proposal ledger rows are written by their senders, untagged), then
   the project row (FK cascades carry the children exactly as
   `discard_project` relies on) and `storage.delete_project_prefix` for
   each. Also the `rfp_created_projects` rows (cascade, then explicit).
2. `general_contractors` / `gc_contacts` with the session tag, only when no
   OTHER project still references them (`project_gcs` for a GC,
   `project_gc_contacts` for a contact); a GC the session merely attached
   is never touched. Kept rows are reported with why.
3. `rfp_harvests` with the tag and every `rfp_ingest_runs` row with the tag
   or linked from those harvests: each run through `rfp_ingest.delete_run`
   (both storage prefixes, then the row); a run the sandbox refuses (still
   running, a live job) is kept and reported as "storage objects kept:
   ...". The harvest rows go whatever happened to their runs.
4. `rfp_emails` with the tag (cascade sightings, training rows, match rows).
5. `ingested_emails` with the tag, their attachment objects first through
   `storage.delete_file` (the rows cascade).
6. `email_log` rows with the tag (the redirected `send_mail` rows).
7. `notifications` cascade with their projects (recorded in the report's
   `notes`).

The session row and `rfp_test_events` stay (the events' row references
null out). Report shape: `{ deleted: { projects, gcs, gc_contacts,
harvests, ingest_runs, rfp_emails, ingested_emails, email_log }, kept: [{
table, id, why }], errors: [<step: message>], notes: [] }`, stored on
`cleanup_report` with `cleanup_started_at` / `cleanup_finished_at`; events
`session.cleanup_started` and `cleanup_finished`. Each step is isolated:
a failure is reported and the next step still runs. Never deletes anything
without the tag. Never runs on the active session (409
`rfp_testing_session_active`).

---

## 10. Migration `0131_rfp_testing.sql` (dev only)

- `rfp_test_sessions`, `rfp_test_events` (section 3.1, 7.1), RLS on both.
- `test_session_id uuid references rfp_test_sessions(id) on delete set null`
  added (if not exists) to: `rfp_emails`, `rfp_harvests`, `rfp_ingest_runs`,
  `rfp_created_projects`, `projects`, `general_contractors`, `gc_contacts`,
  `ingested_emails`, `email_log`; partial indexes `where test_session_id is
  not null` on each.
- `rfp_emails.forward_meta jsonb`.
- `email_log.redirected_to text`.
- `notify pgrst, 'reload schema'` at the end (PostgREST reload after DDL, the
  project's migration ops note).

Apply to the DEV project (`bpidntbyvoooqvaispup`) only.

---

## 11. Frontend: `/rfp-testing`

Gate: `features.rfp_testing && profile.is_dev && profile.role === 'it_admin'`;
otherwise the page renders the same "not available" block the AI monitor
uses. Sidebar item "RFP testing" (dev only, feature flag `rfp_testing`, role
`it_admin`), placed after "Ingestion sandbox". Design system: the existing UI
kit in `components/ui` (squared-off neumorphic navy on light, no glass).
Strings in the `rfpTesting` namespace of all six catalogs (English in all
six, like the other RFP namespaces).

Layout:

1. **Session bar** (top, sticky): status pill (Inactive / Active since 3:12 PM
   PT, by Tom / Ended), the config summary (watching symone, accepting
   t.moorejr, redirecting to baseballtom33@gmail.com), the Activate button
   (opens a confirm modal with name field, auto-create toggle, and the plain
   list of what happens: what is watched, what is paused, where mail goes),
   Deactivate (confirm), auto-create toggle while active, session picker for
   history (newest first), Clean up on an ended session (confirm, shows the
   report after). Heartbeat text: "Intake checked symone 12 s ago; filer 9 s
   ago" from `heartbeat`, red when older than 3x the poll interval. Paused
   banner while active listing the paused mailboxes, NGEM and the filer
   mailbox, with the note about queued harvests.
2. **Tabs**: Timeline, RFP emails, Projects, Mail in, Mail out, Ignored.
   - **Timeline**: live list of events (newest first), filter chips by
     source and level, search box on title; each row expands to a detail
     panel that renders the interesting keys first (per kind, a small
     renderer map: classify shows answer, confidence, reasoning, then the
     prompt in a collapsible; match shows the candidate table; harvest file
     rows show the verdict chips) and the raw JSON in a collapsible
     `<pre>`.
   - **RFP emails**: one card per row with the step strip (ten chips
     coloured by state, clickable to open that step's event), the effective
     sender and subject with the raw forward beneath in muted text, method,
     status, flag reason, last error, links to the existing `/rfp-emails`
     detail (for the human actions) and to the created project.
   - **Projects**: the session's projects; clicking one opens the project
     view (from `/rfp-testing/projects/{id}`): header (number, name, stage,
     owner role, dates), lane / category state, GCs and contacts, files table
     (name, category, size, source), filed emails, stage events, notifications,
     outbound mail, RFP flags, a link to the real project page.
   - **Mail in**: the filer rows with R1 / R2 / R3 outcome and reasoning and
     the assigned project.
   - **Mail out**: redirected sends: time, intended recipients with their
     resolved identity, subject, project, attachments, preview (expand).
   - **Ignored**: what arrived in the test mailbox and was dropped, with the
     reason.
3. **Live updates**: poll `GET /rfp-testing/state` every 3 seconds with the
   `after` cursor; append events; refresh the selected tab's list every 6
   seconds (or on any new event whose source matches the tab). Stop polling
   when the tab is hidden (`document.visibilityState`). Follow the strict
   react-hooks lint rules (no sync setState in effects, no ref reads in
   render); `npm run lint` is the gate.

---

## 12. Tests and verification

Backend (`tests/test_rfp_testing.py` and additions beside the touched
services):

- gates: 404 when off, 403 for non-dev and for dev with another role, 200 for
  dev it_admin; activation 409 on a second active session; each preflight
  422.
- sync filter: in test mode the test mailbox is the only mailbox, only the
  test sender after `started_at` inserts, others record `ignored`; normal
  mode is unchanged (existing tests keep passing).
- sweep filter: normal mode never touches tagged rows; test mode never
  touches untagged rows; ended-session rows are frozen.
- unwrap: Outlook, Original Message, Gmail and Apple blocks; FW prefixes;
  unparseable date keeps the forward's; no block yields `none`; the 5.3
  re-check produces `failed / test_listing_skip`; `.eml` parsing and the
  `eml:` locator.
- redirect: `send_mail` message rewrite (recipients, subject, text body,
  header with resolved identities, attachments kept, inline images dropped),
  `send_draft` PATCH-then-send, refusal on empty redirect, `email_log`
  columns, event recorded; inert when no session is active.
- record never raises; detail capping.
- cleanup: deletes only tagged rows, keeps shared GCs, refuses on active.
- filer: mailbox switch, inbox only, sender and time filters, tag, sweep
  filter.
- NGEM tick no-op while active.

FE: `npm run lint`, `tsc`, and a Playwright screenshot pass of every tab
with the dev server (the headless screenshot recipe in memory).

Live verification (the build's final step, on the dev server at :5051 / :4500
with the local .env values): activate, forward one real Procore invitation
and one organic GC invitation from t.moorejr to symone, watch both run
through to `create`, press Create project, forward an addendum reply to
symone and see it file into the created project, send an RFQ from that
project and see the redirected plain-text copy arrive at the gmail address,
deactivate, confirm the four real mailboxes resume, clean up, confirm the
rows are gone.

---

## 13. Build record

Backend built 2026-09-16 on branch `build` (uncommitted, dev only). Nothing
was applied to any database: `0131_rfp_testing.sql` is written and waits
for the orchestrator to apply it to the DEV project. The frontend (section
11) is a separate build against this document.

### What was built

- `supabase/migrations/0131_rfp_testing.sql`: sections 3.1, 7.1 and 10
  (the GC table is `general_contractors`).
- `app/core/config.py`: the six `rfp_testing_*` settings, the
  `_validate_rfp_testing` boot validator, the production refusal in
  `_validate_production`. `app/core/features.py`: `rfp_testing` in
  `enabled_map`. `app/core/error_codes.py` + `docs/ERROR_CODES.md`: the
  eight `rfp_testing_*` codes.
- `app/services/rfp_test.py` (new): sessions (`active_session`,
  `activate` + `preflight`, `end_session`, `set_auto_create`, `heartbeat`,
  `reset_delta_links`, `poll_seconds`, `message_filter`), the ledger
  (`record`, `cap_detail`, `events_for`, `session_for_email`), the unwrap
  (`unwrap_forward`, `unwrap_fields`, `parse_inline_forward`,
  `parse_address`, `parse_forward_date`, `strip_forward_prefixes`,
  `fetch_eml_bytes`, `parse_eml`, `eml_part_payload`), the redirect
  (`redirect`, `resolve_recipients`, `display_for`, `to_plain_text`,
  `build_header`, `record_redirected`, `RedirectRefused`), `cleanup`,
  `step_chips`.
- `app/routers/rfp_testing.py` (new) mounted in `app/main.py`: the eleven
  routes of section 8, `require_rfp_testing` (router-level 404),
  `require_dev_it_admin`, the catch-all limiter. `app/main.py` also starts
  both pollers when the bench is on.
- `app/services/rfp_email_ingest.py`: `poll_once` / `polling_loop` /
  `_sync_mailbox` / `_insert_from_delta` / `should_skip_sender
  (allow_addresses)` / `_sweep (session)` / `_process_email (auto_create)`
  / `_step_create (auto_create)` for sections 4.1 and 4.4; the unwrap and
  the 5.3 re-check in `_step_received` (`_test_listing_skip_reason`,
  `_list_attachment_meta(with_ids=)`); the section 5.3 auth rule; every
  capture point of 7.2 (`_terminal` takes `row=` at all sixteen call sites
  and records `terminal` itself; `_record_retry`, `_record_extract`,
  `_record_match`, `_record_human`).
- `app/services/email_ingest.py`: section 4.2 (`poll_once`, `polling_loop`,
  `_sync_folder`, `_insert_from_delta`, `process_pending`) and the filer
  capture points (`_record_round`, `fetched`, `assigned`, `unknown`,
  `manual_assigned`, `unassigned`).
- `app/services/rfp_portal_ingest.py`: the section 4.3 no-op.
- `app/services/rfp_harvest.py`, `rfp_email_harvest.py`, `rfp_ingest.py`:
  the tags (`enqueue`, `_find_or_create_harvest`, `create_harvest_run`), the
  `eml:` locator, listing and download, the harvest events (`started`,
  `file`, `link`, `finished`, `parked`, `retry`, `failed`).
- `app/services/rfp_create.py`: the tags on the project, the record, the
  GC and the contact; the `created` and `failed` events
  (`create_from_email` wraps `_create_from_email`).
- `app/services/graph_email.py`: the redirect in `send_mail` and
  `send_draft` (`_redirect_draft`, `_active_test_session`); `send_draft`
  gained `project_id` / `rfq_id`, passed by `rfq_sending` and `rfq_nudges`.
- `bdr_be/.env`: the four `RFP_TESTING_*` lines. `tests/conftest.py` pins
  `RFP_TESTING_ENABLED=false` for the suite.

### What was tested

`tests/test_rfp_testing.py` (63 tests): the gates through a TestClient
(404 before auth, 401 with the flag on), the route table and its
dependencies, `require_dev_it_admin`, the features map, the boot validators
and the prod guard; every preflight code, activation (insert, delta reset,
event, 409), end (idempotent), the auto-create PATCH, `active_session` free
while off, the sync-prefix pins, `message_filter`, `poll_seconds`; `record`
never raising and the detail cap; the intake sync in test mode (only the
test mailbox, one-day lookback, the four ignore reasons, the tag, `listed`,
the heartbeat, the delta token), normal mode unchanged, test mode with no
real mailbox listed, `should_skip_sender(allow_addresses)`, the sweep
filter in both modes and with the switch off; the four forward blocks, the
prefixes, the address forms, the date forms, `unwrap_forward` inline / none
/ eml, `unwrap_fields`, `parse_eml`, `eml_part_payload`, the `eml:` locator
download and listing; `_step_received` on a test row (writes and events),
no block (internal forwarder fails with `test_listing_skip`), the vendor
re-check, a real row untouched, a full walk to `done` recording every step,
the auth rule; `step_chips`; `send_mail` redirect (recipients, subject,
header lines, attachments, inline dropped, ledger columns, event), refusal
on an empty address, no session, `send_draft` PATCH / DELETE / send order,
no Supabase while off, refusal before the PATCH; `resolve_recipients`;
cleanup (refused on active, exact per-table counts, shared GC and refused
run kept, storage calls); the filer in test and normal mode and its sweep
filter; the NGEM no-op; the state route (shape, counts, cursor), the
session routes' error mapping, the emails route with chips; the harvest
tag on `enqueue` and `create_harvest_run` and the `file` event guard.
`tests/test_rfp_create.py` gained the creation tagging test.
`tests/test_feature_flags.py` gained the `rfp_testing` key; four
`send_draft` stubs in `test_rfq_sending.py` / `test_rfq_nudges.py` accept
keyword arguments.

Full suite: 4578 passed, 1 skipped, 1 failed
(`test_rfp_emails_router.py::test_match_stats_answers_the_services_tally`,
the known env-dependent failure: the local `.env` sets
`RFP_MATCH_AUTO_MERGE_ENABLED=true`; it fails without this build too).
`ruff check app tests`: clean on every file touched (four pre-existing
findings elsewhere, untouched).

Live verification against the dev server (the last paragraph of section
12) is NOT done here: it needs migration 0131 applied to dev and a real
forward, both the orchestrator's.

### Deviations, and why

- `require_dev_it_admin` sits on `get_current_user`, not on `require_dev`,
  so both refusals carry `rfp_testing_forbidden` (section 2 updated).
- The preflight checks the redirect address before the Graph call (cheap
  checks first; section 3.2 order updated).
- `rfp_testing_unwrap_scan_chars` was added to the settings block (the
  design named the env var in 5.1 but not the setting).
- In test mode the initial mailbox pull looks back one day (the design
  called the window irrelevant; one day keeps the first tick's `ignored`
  list short).
- `intake.ignored` also records the vendor / blocked / RFQ-thread drops of
  the raw From (the design said they were inert for `t.moorejr`; recording
  them costs nothing and keeps the Ignored tab honest).
- The auth step passes a test row on the tenant's trust when the policy
  reads anything but pass (section 5.3): the design asserted the forward's
  header passes, but Exchange Online stamps no SPF / DKIM / DMARC pass on
  intra-tenant mail, so without this every forward would stop at
  `flagged_auth`. Real rows are unchanged.
- `parse_forward_date` also reads the three clients' wall-clock forms as
  Pacific (the design kept only `parsedate_to_datetime`); a naive RFC parse
  is read as Pacific too.
- The `eml:` locator carries the mailbox and message id
  (`eml:{mailbox}|{message_id}|{attachment_id}|{index}`) since the MIME
  must be re-fetched from a specific message; it is resolved by
  `EmailFileSession.download`, and the eml row's listing comes from
  `forward_meta.eml_parts` rather than a second Graph listing.
- The GC table is `general_contractors`, not `gcs` (sections 4.4, 10).
- `send_draft` deletes the draft's inline attachments (the design only said
  they are dropped) and takes `project_id` / `rfq_id` for the event; the
  `[TEST for ...]` prefix uses the resolved name without its role suffix.
- The `email_log` columns are written only while a session is active (a
  plain deployment must not name columns it does not have).
- Cleanup also deletes `email_log` rows of the session's projects (the
  draft senders write untagged ledger rows keyed on the project) and keeps
  a run the sandbox refuses to delete, reported; `notifications` cascade.
- Events beyond the 7.2 table: `harvest.parked` / `retry`, `create.parked`,
  `session.auto_create`, the human kinds named above.
- The rate limit is the catch-all `RateLimitScope.DEFAULT` budget (240 per
  minute), the most generous one that exists; no new scope was added.
- Section 12's "FE" and "Live verification" items are outside this build.

**Frontend** (2026-09-16, built against section 8 as written, before the
router existed; untested against the live API). `lib/rfpTesting.ts` holds
the section 8 types, the fetch helpers, the error-code list and the gate
(`canSeeRfpTesting`); `lib/features.ts` gains `rfp_testing` (fails closed).
The page is `app/(app)/rfp-testing/page.tsx` (gate, the 3 s state poll with
the `after` cursor, the event log, tab wiring) with its pieces beside it:
`SessionBar.tsx` (status pill, config summary, Activate modal with the
consequence list, Deactivate and Clean up confirms, the cleanup report,
auto-create toggle, session picker, heartbeat with the 3x stale colouring,
paused banner), `tabs.tsx` (the six tabs, the project view, the 6 s list
poll hook driven by per-source event counts), `EventDetail.tsx` (the
classify / extract / match / fetched / harvest file / mail_out renderers
plus the generic key-value + raw JSON fallback, and the side panel the step
chips open) and `shared.tsx`. Sidebar item "RFP Testing" after the sandbox
(`devOnly`, `featureFlag: "rfp_testing"`, `roles: ["it_admin"]`, carried
over when Bidding is off). Strings in the `rfpTesting` namespace of all six
catalogs (270 keys plus `nav.rfpTesting`, English everywhere). Gate
rendering: a non-dev is redirected home like the sibling pages; a dev
without the flag, or a 404 from the state route, gets the "not available"
block; a dev in another role, or a 403, gets the line about the role
switcher. Choices where section 8 left room: `SessionSummary` is read as a
subset of `Session`; the event `detail` keys are read with fallbacks
(`answer`/`llm_answer`, `candidates[].project_number|number`, `to[]` as
strings or `{address, display}`, and so on) so the renderers degrade to
the generic view rather than fail; the /rfp-emails link uses that page's
existing `?email=<id>` deep link; the timeline renders the newest 400
matching rows. `npm run lint` (0 errors) and `npx tsc --noEmit` clean.

### Live run (2026-09-16 evening, orchestrator)

Migration 0131 applied to the DEV project (`bpidntbyvoooqvaispup`) only.
Driven over the API as the e2e dev account switched to IT Admin, and
through the real page headless (login form, every tab screenshotted).

- Gates: 403 `rfp_testing_forbidden` as Executive, full state as IT Admin;
  `/rfp-testing/state` answers 401 (not 404) unauthenticated, so the
  switch is live.
- Activation "Bench smoke 1": the preflight reached
  `symone@g3electrical.com` (the mailbox exists and the app registration
  covers it). Both heartbeats arrived on the first test-mode tick (up to
  120 s after activation when the loops were mid-sleep; 15 s after that).
- A synthetic Outlook-style forward sent from `t.moorejr` to `symone`
  through Graph (original sender `estimating@summit-test.example`, a GC
  domain on dev; a two-page drawing PDF attached) ran the whole intake in
  about 20 s from listing: `fetched (inline)` with the original sender,
  auth pass, six keywords, classify yes 0.95, authorized by `gc_domain`,
  method `organic`, extract (name + GC), match `new`, harvest accepted the
  PDF, parked at `create`. The filer saw the same forward and filed it as
  Unknown (expected: no project yet).
- Create project (the `/rfp-emails` button endpoint) made `26.9.7123
  Desert Vista Community Center`, tagged, in Go/No-Go, bid date parsed to
  Oct 8 2:00 PM PT, the PDF promoted as `other`. The two notifications
  (Executive, Estimating Admin) went to the redirect address as plain
  text with `[TEST for Thomas Moore]` / `[TEST for E2E Estimating Admin]`
  subjects and the header block.
- A second forward carrying the project number in the subject: the intake
  classified it `no` 0.90 (`flagged_llm_no`, correct: an addendum, not an
  invitation) and the filer's R2 found the number and filed it INTO the
  project.
- `send_draft` redirect, exercised directly with a real HTML draft (To a
  GC contact address, CC a profile, one attachment): recipients resolved
  with identities, CC captured, attachment kept, body flattened to text,
  `[TEST for ...]` subject. An RFQ bulk-send could not be completed
  because this machine lost its route to the EC2 model server for about
  twenty minutes (the RFQ body variation needs it; the server itself was
  fine, the user confirmed and a later probe answered in 0.3 s); nothing
  to do with the bench.
- End: idempotent, cleanup refused while active (409), cleanup after end
  deleted 1 project (storage prefix included), 1 harvest, 1 ingest run, 2
  rfp_emails, 2 ingested_emails, 2 email_log rows; the shared Summit GC
  and the 39 events stayed. The four real mailboxes synced again on the
  next normal tick.

Fixes made from what the run showed:

- `parse_forward_date`: `parsedate_to_datetime` reads the Outlook "Sent:"
  line but drops the AM/PM marker ("2:10 PM" became 02:10). The explicit
  wall-clock forms now win and the lenient naive parse is the last resort
  (section 5.1 unchanged in substance; test extended with PM cases).
- The state, session projects and project detail routes fan their
  independent reads out concurrently (`_gather`): state 1.5 to 3 s down to
  under 1 s, project detail 5 to 10 s down to about 2.7 s (the remainder is
  the shared notification-log builder). The page polls these every 3 and
  6 s, so this is what keeps it live rather than spinning.
- `rfp_email_ingest.release_leases` runs on lifespan shutdown: the intake
  and NGEM leases are sized to cover an LLM wait (33 minutes), so a
  `--reload` restart or a redeploy mid-tick used to leave a dead process's
  lease in place and the next worker waited it out; on the dev server that
  made the bench look dead after every code save. Pre-existing, now fixed
  for both pollers.

Not exercised live: the forward-as-attachment (`.eml`) path (unit-tested
only), and an RFQ / nudge send end to end (blocked by the local network
blip above; the `send_draft` redirect itself was exercised).
