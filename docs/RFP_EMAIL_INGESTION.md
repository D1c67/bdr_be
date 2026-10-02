# RFP Ingestion: Email Intake

Design record for the email-intake slice of RFP Ingestion: watching a fixed set
of company mailboxes, authenticating and classifying every inbound message,
deciding whether the sender is authorized, and labeling the invitation method.
It hands each accepted message to the later slices (the match step and the
information harvest), which are NOT part of this sprint. The sandbox and
sanitization slice is documented separately in `RFP_INGESTION_SANDBOX.md`.

Status: design approved 2026-09-09; BUILT 2026-09-10 and running against the
dev database (migration 0120 applied to dev only). Not in staging or
production. Section 12 records what the first live run showed.

Naming, used everywhere: setting prefix `rfp_email_` (env prefix
`RFP_EMAIL_INGESTION_`), service `app/services/rfp_email_ingest.py`, router
`app/routers/rfp_emails.py` under `/rfp-emails`, tables `rfp_emails`,
`rfp_email_sightings`, `rfp_authorized_senders`, LLM feature `rfp_classify`,
`GET /features` key `rfp_email_ingest`, FE page `/rfp-emails`, settings tab
"RFP Ingestion", FE namespace `rfpEmails`.

Master switch: `RFP_INGESTION_ENABLED` (setting `rfp_ingest_enabled`) turns on
BOTH RFP Ingestion slices; the older `RFP_INGEST_ENABLED` spelling is still
accepted. The email intake additionally requires a non-empty
`RFP_EMAIL_INGESTION_INBOXES_ALLOWED`; the derived setting
`rfp_email_ingestion_enabled` and the `GET /features` key `rfp_email_ingest`
express that combination.

---

## 1. Decisions locked in (2026-09-09)

| Topic | Decision |
|---|---|
| Watched mailboxes | Only the addresses in `RFP_EMAIL_INGESTION_INBOXES_ALLOWED`. Inbox folder only. Each is a separate mailbox (tmoore is distinct from t.moorejr). |
| Internal senders | Ignored, never stored. Forwarded invites from colleagues are therefore not seen. Deferred to a later sprint. |
| Vendor senders (2026-09-14) | Ignored, never stored, exactly like internal senders. Any `vendor_contacts` address, and any vendor contact's domain (with subdomains). Suppliers quote TO us and never invite us. A vendor rep on a public provider skips only that address; a domain that is also a GC domain is not skipped (ambiguous, so it goes through the pipeline). |
| RFQ reply threads | A message whose Graph `conversationId` matches an `rfq_sends` row is ignored. The bids mailbox stays in the list. |
| Email authentication | Verdicts are read from the `Authentication-Results` header Exchange Online already stamped. No DNS lookups. Flag on a DMARC or compauth fail, or when no ALIGNED pass exists. A DMARC or compauth pass, or an SPF / DKIM pass whose domain aligns with the From domain, passes (3.3, tightened 2026-09-30). |
| Keyword gate | Case-insensitive, word-boundary matching over subject plus plain-text body. |
| LLM step | Runs before the authorization check, so the unauthorized review list only holds messages that look like real invitations. |
| Confidence threshold | Env value `RFP_EMAIL_INGESTION_CONFIDENCE_THRESHOLD`, default 0.85. The model may be upsized (Qwen 27B class) without code changes. |
| GPU off | The classify step waits. No cloud fallback for this feature. Nothing else in the pipeline stalls. |
| Free-mail providers | A GC contact at gmail.com or similar never authorizes the whole provider domain. Only the exact address is authorized. Production data does not currently have this problem; the guard is a safety net. |
| Cross-mailbox duplicates | One row per message (by Internet Message-ID), with one sighting row per mailbox that received it. Race-safe by unique index plus insert-on-conflict. |
| Authorization precedence | Locked platform rules, then GC domain (organic), then user-added rules (general), then the human "continue" on an unauthorized message (nonorganic). Verified by test after build. |
| Attachments | Names, types and sizes are recorded from the cheap Graph listing. Content is never fetched by this slice. |
| Body rendering | Plain text only, requested from Graph as text. Links are shown as inert text. |
| Review queue roles | Estimating Admin primarily. IT Admin, Executive and both Estimating Engineer focuses may also act. Accountant and Estimator never see it. |
| Authorized-sender management | Executive, Estimating Admin and IT Admin. Locked rules are IT Admin only. |
| Lookback on first start | 3 days, deployment-forward, on dev and on the eventual production first start. See section 6. |
| Platform addresses | The given platform addresses are seeded as locked rules (after 2026-09-16: Procore, plus the two PipelineSuite GC domains seeded by 0129; after 2026-10-01: plus `smartbidnet.com`, seeded by 0148). More can be added later as locked rules by the IT Admin. |
| Blocked platforms (2026-09-15, 2026-09-16) | BuildingConnected, NGEM and PlanHub are not invitation methods. Mail from buildingconnected.com, ionwave.net, planhub.com and planhubprojects.com (and subdomains) is sanitized out at listing time via `RFP_EMAIL_INGESTION_BLOCKED_DOMAINS`, like internal and vendor senders: no row, no LLM call. Migration 0124 removed the BuildingConnected and NGEM seed rules and rows and narrowed both method check constraints; migration 0128 did the same for PlanHub. |
| GC portals (2026-09-16) | `gc_portal` is the method for a GC that invites through its own bidding portal. Every such portal is different, so the harvest step picks the scraper by the sender's domain (`RFP_HARVEST.md` 2.3). The method is granted by a locked rule the IT Admin adds on that GC's sending domain; locked rules outrank the GC-domain source, which is how the portal rule beats the same GC's organic match. Migration 0127 widened both check constraints; no rows changed. Scrapers are built per GC and none exist yet: until one is registered a gc_portal row drains to done unharvested, like organic. |
| PipelineSuite portals (2026-09-16) | `pipelinesuite` is the method for a GC whose plan room is a PipelineSuite (PreconSuite) portal at `<gc>.pipelinesuite.com`. Unlike `gc_portal`, one method-keyed harvester serves every such GC: the invitation email comes from the GC's own domain and carries the portal host, a Project ID and a Security Key, and that is all the harvester needs (section 3.9, `RFP_PIPELINESUITE.md`). Granted by a locked domain rule on the GC's own domain; migration 0129 widened both check constraints and seeded `cgandbinc.com` and `shfcontracting.com`. |
| SmartBid (2026-10-01) | `smartbid` is the method for invitations sent through ConstructConnect's SmartBid. Every one comes from the platform's own address (`notifications@com2.smartbidnet.com`), so, like Procore and unlike PipelineSuite, it is granted by ONE locked domain rule on the platform domain (`smartbidnet.com`, covering `com2.smartbidnet.com` on the label boundary), and one method-keyed harvester serves every GC that sends through it (section 3.9.1, `RFP_SMARTBID.md`). Migration 0148 widened both check constraints and seeded the rule. |

---

## 2. Pipeline overview

```
Graph delta (per mailbox, Inbox)
  |
  |-- from is internal domain ............... ignored, no row
  |-- from is a blocked platform domain ..... ignored, no row
  |-- from is a vendor sender ............... ignored, no row
  |-- conversationId in rfq_sends ........... ignored, no row
  |-- Internet Message-ID already stored .... sighting row only
  v
rfp_emails row: status = received
  |
  v  fetch      GET message: text body + internetMessageHeaders + attachment listing
  v  auth       parse Authentication-Results ........ fail -> flagged_auth
  v  keywords   word-boundary scan .................... none -> flagged_no_keywords
  v  classify   LLM yes/no/undetermined + confidence
  |               yes  >= threshold -> authorize
  |               no   >= threshold -> flagged_llm_no
  |               else              -> review_llm  (human: yes -> authorize, no -> rejected_by_review)
  v  authorize  address / domain / GC-domain rules .... unauthorized -> flagged_unauthorized
  |                                                       (human: continue -> method as nonorganic)
  v  method     organic | procore | pipelinesuite | smartbid | gc_portal | general | nonorganic
  v  done       (parked here for the future match step and harvest step)
```

`status` always names the NEXT step to run, exactly as the PM ingestion does.
A crash, deploy or lease loss leaves the row at its recorded step and the
every-tick sweep resumes it. Every terminal write also records the step that
decided it (`decided_at_step`) and a `flag_reason`.

Status vocabulary:

- Pending (the sweep picks these up): `received`, `auth`, `keywords`,
  `classify`, `authorize`, `method`.
- Waiting on a human: `review_llm`, `flagged_unauthorized`.
- Terminal: `done`, `flagged_auth`, `flagged_no_keywords`, `flagged_llm_no`,
  `rejected_by_review`, `failed`.

---

## 3. Step by step

### 3.1 Delta sync

- One background loop, started from the lifespan like the other pollers, gated
  by `RFP_INGESTION_ENABLED`, the mailbox list being non-empty, and the Graph
  client id being set.
- One fenced lease row in `graph_sync_state` (`rfp-mail:lease`) covers ALL
  mailboxes, with a per-process holder token. The two production uvicorn
  workers can never both sync or sweep. The lease is renewed between mailboxes,
  every 20 sweep rows on the free steps and immediately before every LLM call
  (`RFP_MATCHING.md` section 5; the lease length is
  `RFP_EMAIL_INGESTION_LEASE_SECONDS`); a runner that loses its lease stands
  down.
- Per mailbox, a delta link is stored at `rfp-mail:{mailbox}:inbox`. The
  select is `id, conversationId, internetMessageId, from, toRecipients,
  ccRecipients, subject, receivedDateTime, hasAttachments`. No body on the
  listing.
- Delta is persisted only after the batch is durably inserted. On HTTP 410 the
  link is reset with the reset lookback window; re-pulled messages dedup on
  the unique indexes.
- Skips decided from the listing alone, costing nothing:
  - Internal sender: the From domain is in `RFP_EMAIL_INGESTION_INTERNAL_DOMAINS`
    (default `g3electrical.com`) or the From address is one of the watched
    mailboxes. No row is written.
  - Blocked sender (2026-09-15, 2026-09-16, managed from 2026-09-22): the
    From address is an `address` row of `rfp_blocked_senders`, or its domain
    is a `domain` row of that table (subdomains covered on a label boundary:
    `customer.ionwave.net` and `message.planhub.com` yes,
    `fakebuildingconnected.com` no). No row is written and no LLM call is
    spent. The table is read once per mailbox sync, live like the vendor and
    GC sets, so a block taken a minute ago applies on the next tick; if it
    cannot be read the sync aborts without persisting its delta token, which
    fails CLOSED (nothing ingested) rather than open.
    `RFP_EMAIL_INGESTION_BLOCKED_DOMAINS` still exists and is unioned with
    the table, but its default is now EMPTY: anything listed in the
    environment cannot be unblocked from the page, so a non-empty default
    would make an unblock look like it worked while the poller kept dropping
    the mail. Migration 0133 seeds the four domains that used to be that
    default (BuildingConnected, NGEM/IonWave and both PlanHub domains) as
    LOCKED rows, so their behaviour is unchanged and they are now visible and
    removable by an IT Admin.
  - Vendor sender: the From address is a `vendor_contacts` email, or its
    domain is (or is a subdomain of) a vendor contact's domain. No row is
    written. The vendor set is read once per mailbox sync
    (`vendor_senders`), live like the GC domains, so a contact added a
    minute ago is dropped on the next tick. Internal domains never count;
    a contact at a public provider contributes only the exact address; a
    domain that is also in `gc_contacts` contributes only the exact address
    too, because silently dropping a real invitation is the worse mistake.
  - RFQ thread: `conversationId` exists in `rfq_sends.conversation_id`. No row
    is written. This removes vendor quote replies and nudge replies from the
    bids mailbox (the vendor-sender skip above catches the same replies even
    when the RFQ was sent from another environment, plus a supplier opening
    a fresh thread).
- Blocking a sender (2026-09-22). The Executive, the Estimating Admin, the
  IT Admin and any dev account block a sender from Settings -> RFP Ingestion
  or from the open message in the review queue. Two scopes, named in the
  dialog rather than implied: an `address` block stops that one person and
  leaves their colleagues coming through; a `domain` block stops everyone at
  the company, including addresses nobody has seen yet. A `domain` block on a
  public mailbox provider is refused (blocking gmail.com would silently drop
  every GC contact who uses it), as is a block on our own internal domain.
  From the review queue the blocked value is derived from the STORED
  `from_address`, never from the request, and the blocker must type it back
  exactly; the server checks the typed value again, so a mistyped
  confirmation is a 400 `rfp_block_confirm_mismatch` and nothing is written.
  Blocking also parks everything from that sender still IN FLIGHT (every
  pending step and every human lane) at the terminal status
  `blocked_sender`, with `flag_reason` `sender blocked (<value>)` and
  `decided_at_step` remembering where each row was standing. Terminal rows,
  including anything already merged into a project or that created one, are
  never rewritten. Unblocking lets new mail through from the next tick and
  deliberately does NOT reopen the rows a block already parked.
- Insert: `rfp_emails` on conflict (`internet_message_id`) do nothing, then
  `rfp_email_sightings (rfp_email_id, mailbox, graph_message_id)` on conflict
  do nothing. Message-IDs are normalized (trimmed, angle brackets removed,
  lowercased). A message with no Message-ID uses `graph:{mailbox}:{id}` so it
  is still unique.
- The first mailbox that sees a message becomes `primary_mailbox`; the fetch
  step reads the body from that mailbox. If that mailbox later loses the
  message (deleted by a user) the fetch falls back to any other sighting.

### 3.2 Fetch

- One GET per message on the primary mailbox with
  `$select=body,bodyPreview,internetMessageHeaders` and
  `Prefer: outlook.body-content-type="text"`. This is the only per-message
  call besides the attachment listing, so the authentication data is free.
- `body_text` is capped at `email_body_max_chars` (existing setting, 100k).
- If `hasAttachments`, the cheap attachment listing (`id, name, contentType,
  size`, plus the OData type) is stored as `attachments_meta` JSON. Content is
  never requested. Reference (cloud link) attachments are recorded by name
  only; their URLs are not resolved in this slice.
- A 404 from Graph (message removed) marks the row `failed` with reason
  `message_gone`.

### 3.3 Authentication verdicts

- The parser reads the top-most `Authentication-Results` header on the message
  and no other. Exchange Online prepends its header on arrival, so that is the
  one the tenant stamped: the authserv-id ends in
  `mail.protection.outlook.com` or the header carries `compauth=`. Headers
  lower down are never consulted, because a sender can forge them, in
  Exchange's own shape included. If the top header is not the tenant's, the
  message fails closed and is flagged for review.
- Extracted: `auth_spf`, `auth_dkim`, `auth_dmarc`, `auth_compauth`, each
  stored as the raw result token (`pass`, `fail`, `none`, `softfail`,
  `temperror`, `permerror`, `bestguesspass`, absent), plus the domain each
  pass was judged for: `auth_spf_domain` (the domain of `smtp.mailfrom=`)
  and `auth_dkim_domain` (`header.d=`), both lowercased, null when absent
  (migration 0146). The full header text is stored in `auth_raw` for the
  later review screen.
- Policy (`auth_verdict`), security review 2026-09-30. A pass is evidence
  about the domain it was computed for, so it only counts when that domain
  ALIGNS with the RFC 5322 From domain (relaxed alignment: the same domain,
  or one a subdomain of the other on a label boundary). Before this, any
  SPF or DKIM pass passed the message, so a sender with SPF on a domain of
  their own could forge the From at a GC domain that publishes no DMARC
  record and be authorized as that GC.
  - `fail` when no tenant header exists at all.
  - `fail` when `dmarc` is `fail` or `compauth` is `fail` (Exchange found
    the From spoofed; flag reasons `dmarc_fail`, `compauth_fail`).
  - `pass` when `dmarc` is `pass` or `compauth` is `pass`: Exchange's own
    aligned verdicts, the primary signal.
  - `pass` on an aligned SPF pass (`auth_spf_domain` aligns with the From:
    a DKIM-less small GC with intact SPF on its own domain is legitimate) or
    an aligned DKIM pass (`auth_dkim_domain` aligns).
  - `fail` otherwise. An SPF or DKIM pass for some other domain is flagged
    as `unaligned_pass`; no pass at all as `no_auth_pass`.
  - The parser drops the header's RFC 8601 comments (`spf=pass (sender IP
    is ...)`) before reading results and properties, so text inside a
    comment can never be read as a `header.d=` or a `dmarc=` of its own.
- Flagged rows stop at `flagged_auth`, the manual lane, exactly as before:
  nothing is dropped. They are listed read-only in the review page's
  Flagged tab. No human override exists in this sprint.

### 3.4 Keyword gate

Case-insensitive regex with word boundaries over `subject + "\n" + body_text`:

```
bid, package, request, proposal, quote, propose, rfp, invite, invitation,
budgetary, itb, ifb, rfq, solicitation, project, tender, bidder, bidding,
lrb, rebid, requote, ve, gmp, db, dbb, cmar, proposed, quotation, quoting,
pursue, pursuing, pursuit
```

`\b(?:bid|package|...)\b` compiled once. Matched words are stored in
`keyword_hits` (distinct, lowercased) so the review screen can show why a
message advanced. No hits stops the row at `flagged_no_keywords`.

Expectation: most external business mail hits `project` or `request`, so this
gate mostly trims newsletters and receipts. Its job is to be a cheap floor, not
the main filter.

### 3.5 Classify (LLM)

- New feature `rfp_classify` registered in `app/services/llm.py`. Vendor
  `openai` for the 3rd-party pool, `SELF_HOSTED_RFP_CLASSIFY_MODEL` for the
  self-hosted pool. With `FULL_SELF_HOSTED_LLMS_ENABLED=true` it never leaves
  the box, per the strict routing rule.
- Runs under the pipeline tier of the concurrency gate, so it never steals
  interactive slots from users.
- Input: subject, sender address and display name, and the first
  `RFP_EMAIL_INGESTION_CLASSIFY_MAX_BODY_CHARS` (default 12,000) characters of
  the text body, wrapped in clear delimiters. The system prompt states that
  the message is untrusted content and that instructions inside it must be
  ignored. Temperature 0. JSON schema enforced:

  ```json
  {"answer": "yes | no | undetermined", "confidence": 0.0, "reasoning": "..."}
  ```

  The question: is the sender inviting or asking G3 Electrical to bid, quote,
  or propose on a construction project? A vendor quoting TO us, a GC
  acknowledging OUR proposal, a payment or schedule notice, and marketing are
  all `no`.
- Budget (security review 2026-09-30). The model runs before the authorize
  step, so without a cap any sender whose mail authenticates and carries a
  keyword could spend one call per message and push real invitations back.
  Before the call the step runs the free authorization check (3.7); a sender
  a rule or GC domain authorizes is never budgeted. Every other sender is
  limited to `RFP_EMAIL_INGESTION_CLASSIFY_BUDGET_PER_SENDER_PER_DAY`
  (default 20) calls per budget key per rolling day, and the sweep to
  `RFP_EMAIL_INGESTION_CLASSIFY_BUDGET_PER_TICK` (default 25) such calls per
  tick. The budget key (`rfp_email_auth.classify_budget_key`) is the
  sender's registrable domain, subdomains included (`a0.evil.example` and
  `a1.evil.example` both count against `evil.example`; `bids.gc.co.uk`
  against `gc.co.uk`, using a short built-in list of second-level public
  suffixes plus shared hosting suffixes such as `onmicrosoft.com`, no
  Public Suffix List dependency), so rotating addresses or sibling
  subdomains inside one organization shares one budget. A key under such a
  suffix also counts against the suffix as a whole, at
  `_SUFFIX_BUDGET_MULTIPLIER` (10) times the per-key budget: whoever owns a
  suffix-shaped domain (`co.de`) can mint a registrable domain per message,
  so `co.de` as a whole stops at 200 calls a day. When the From domain is a
  public mail provider or a consumer ISP mailbox
  (`rfp_email_auth.BUDGET_ADDRESS_DOMAINS`: the list in 3.7 plus comcast.net,
  cox.net, gmx.com and similar) the key is the canonical address instead
  (`canonical_mailbox_address`: lowercased, the `+tag` dropped, and at
  Gmail the dots dropped with googlemail.com folded into gmail.com), so one
  free account cannot spend every Gmail user's budget and
  `spammer+7@gmail.com`, `s.p.a.m.m.e.r@googlemail.com` and
  `spammer@gmail.com` share one budget. An address key is counted with a
  loose `ilike` pattern (`name%@provider`; at Gmail a wildcard between every
  character) and the hits are filtered exactly, read in pages past
  PostgREST's row cap; a key the pages cannot cover, or one that holds a
  character outside `[a-z0-9._-]`, is treated as over budget and waits. A
  provider missing from the list is keyed as one organization, which is
  safe for spend (one budget for the whole provider) but lets one account
  there defer every other unauthorized sender at that provider for a day.
  The daily count keys on `classified_at` (the moment the
  model answered, migration 0147), not on `received_at`, so a backlog that
  is classified today spends today's budget like fresh mail. A row over
  budget stays at `classify` with a `Deferred: ...` note in `last_error`
  and `next_attempt_at` pushed: by the model-wait interval for the per-tick
  cap, and to the moment the oldest counted call leaves the rolling day for
  the per-key cap (the model-wait interval at least), so a flood is not
  re-counted every tick. No attempt is spent and nothing is dropped. The
  row is picked up again once the budget frees, or at once when a rule for
  the sender is added: the learn-back (3.7) clears `next_attempt_at` and the
  note on waiting `classify` rows the new rule covers, and the sender is
  authorized and unbudgeted from then on. Deferred rows can never hold newer
  mail behind them: the sweep serves rows that never waited before rows
  whose wait elapsed (section 5). 0 disables either cap. The test bench's
  rows are never budgeted.
- Prompt `rfp_classify_v2` (2026-09-14) adds two paragraphs for vendor mail
  that is not on the vendor list. The first live weeks showed supplier
  replies (Graybar, Rexel, Codale, Empire, TraStar) classified `yes` at
  0.85 to 1.0 with reasonings such as "initial email explicitly requests G3
  Electrical to provide quotes for the attached BOM": the model read G3's
  own quoted RFQ as the sender's request, and read "are you still planning
  to bid? see attached quote" from a lighting rep as an invitation. v2
  states the direction test (who does the work, who gets paid; a sender who
  would sell material to G3 is `no`), lists vendor cues (delivering or
  revising a quote, unit prices, lead times, submittals, asking for a PO or
  a BOM, a reply to our RFQ subject with a G3 project number, a quotations
  or sales signature), and says that anything quoted from a g3electrical.com
  address is OUR request and never an invitation. Known vendors never reach
  this step at all (section 3.1).
- Stored: `llm_answer`, `llm_confidence` (clamped to 0..1), `llm_reasoning`
  (truncated to 20 words), `llm_model`, `llm_prompt_version`.
- Routing on the answer, threshold `T` from env:
  - `yes` and confidence >= T: advance to `authorize`.
  - `no` and confidence >= T: `flagged_llm_no` (terminal, visible in Flagged).
  - anything else, including `undetermined` and unparseable output: `review_llm`.
- Waiting behavior when the box is off, which is the design center of this
  step:
  - Before calling, the step consults the cached LLM health snapshot
    (`llm_health.cached()`). If the self-hosted provider is down, the row's
    `next_attempt_at` is pushed by `RFP_EMAIL_INGESTION_CLASSIFY_RETRY_SECONDS`
    (default 300) and NO attempt is spent. No request is sent, so no timeout
    is burned. The rest of the tick continues with other steps and other rows.
  - A connection error or timeout on a live call is treated the same way: no
    attempt is spent. These are the "box just went off" cases.
  - Our own AI gate refusing admission (`llm_gate.LlmBusy`, filed as
    `overloaded`) is also a wait: nothing is wrong with the row or the model.
  - Other transient failures (5xx, a provider 503, unusable output twice)
    spend an attempt on the 1 min, 5 min, then 15 min ladder, up to
    `RFP_EMAIL_INGESTION_CLASSIFY_MAX_ATTEMPTS` (default 4), then `failed`:
    about 21 minutes from the first failure to the verdict. The same ladder
    and cap cover fetch, harvest, split and create (`_retry_or_fail`).
  - Rows drain when the box returns, at most one sweep batch (200 rows) per
    tick, gated by the concurrency limiter: rows that never waited oldest
    first, then the rows whose wait elapsed in the order they became due
    (section 5). A weekend backlog of a few hundred messages clears in a
    handful of ticks.
  - Because attempts are not spent while the box is off, a row can wait for
    days without failing. Rows show "waiting for the model" in the UI.

  Failures and alerts (IT Admin, bell plus the mirror email, landing on
  `/rfp-processing?lane=stuck`):
  - `rfp_processing.step_failed`: rows that ran out of attempts. A tick
    collects them (`alert_batch`, wrapped around the email and the portal
    sweeps) and sends ONE bell per IT Admin: the detailed form (subject,
    step, last error, a link to the row) for a single row, a count by step
    for several. Mailbox visibility (0134) applies: a non-dev IT Admin sees
    subjects only from mailboxes they can see and gets a count for the rest,
    with no row link. Bench rows are skipped. Outside a tick (a Retry in a
    request) a failure alerts at once.
  - The portal has no `failed` status: a row at the ladder's end holds at
    its step with a one-hour wait and its attempts pinned at the cap; the
    alert goes out on the way in, and the hourly re-tries hold quietly. A
    portal match that runs out goes to `review_match` and is not alerted
    (the review lane is the signal).
  - `rfp_processing.model_away`: tracked per step (classify, extract, match,
    and split on `bid_split`). A step counts as away when the health probe
    says so OR a call at that step had to wait this tick (a revoked key, an
    empty account, a box that lists its model but hangs, our own gate stuck
    busy: things the probe cannot see). When a step that is away right now
    has been away for `RFP_MODEL_AWAY_ALERT_MINUTES` (default 60) with real
    rows at it, IT Admin gets one alert for the outage. A step's outage ends
    only after it has looked healthy for max(10 min, two model-wait
    intervals), so a flapping probe is one outage, not an away/back pair per
    cycle; rows leaving (a person dismissing them) never ends it. When every
    step has cleared, `rfp_processing.model_back` follows if the alert went
    out. `check_model_away` runs at the end of each intake tick, only while
    the tick still holds the lease and never during a bench session; state
    is graph_sync_state row `rfp-mail:model-away` (JSON in `delta_link`),
    marked alerted before the bell so a failed write cannot re-ring every
    tick. Known gap: a deployment with the portals on and the email intake
    off has no model-away alert (the check rides the email tick).
  - The sweep stamps the model wait on rows it skips behind a down model, so
    they read "waiting for the model" rather than "stalled".

### 3.6 Human review of the LLM verdict

- Rows at `review_llm` appear in the Review tab. The reviewer sees subject,
  sender, recipients, received time, the plain-text body, attachment names and
  sizes, the model's answer, confidence and reasoning, and the keyword hits.
- Actions: "Yes, this is an RFP" moves the row to `authorize`. "No" moves it
  to `rejected_by_review`. Both record `review_decision`, `review_by`,
  `review_at`.
- Every review write is a conditional update (`where status = 'review_llm'`)
  so two reviewers, or a reviewer and the sweep, cannot both act.
- Every decision is captured as a training example (subject, body excerpt,
  model answer, human answer) in `rfp_classify_training`, following the BOQ
  and bid-splitter capture pattern, so the threshold and prompt can be tuned
  against real decisions and a larger model can be measured before switching.

### 3.7 Authorization

Rules live in `rfp_authorized_senders`:

| column | meaning |
|---|---|
| kind | `address` or `domain` |
| value | lowercased address or bare domain |
| method | invitation method granted by this rule (`procore`, `pipelinesuite`, `smartbid`, `gc_portal`, `general`; `buildingconnected` and `ngem` were removed by 0124, `gc_portal` was added by 0127, `planhub` was removed by 0128, `pipelinesuite` was added by 0129, `smartbid` was added by 0148) |
| locked | true for platform rules, pipelinesuite rules, smartbid rules and gc_portal rules; only the IT Admin can add, edit or remove locked rules |
| created_by | user id, null for the seed |

Seed (locked; 0120 seeded four rows, 0124 removed the BuildingConnected and
NGEM rows and 0128 the PlanHub row because that mail is now dropped at
listing time; 0129 added the first two PipelineSuite portals; 0148 added
SmartBid):

| kind | value | method |
|---|---|---|
| domain | procoretech.com | procore |
| domain | cgandbinc.com | pipelinesuite |
| domain | shfcontracting.com | pipelinesuite |
| domain | smartbidnet.com | smartbid |

Evaluation, on the RFC 5322 From address only (never display name, never
Reply-To), and only after the auth step passed:

1. Exact `address` rules.
2. `domain` rules. A domain rule covers the domain itself and its subdomains
   on a label boundary: `procoretech.com` covers `us02.procoretech.com` (the
   regional sender Procore actually uses, seen live), never
   `fakeprocoretech.com`. A rule on a subdomain stays narrow: a rule on
   `bids.example.com` does not cover `example.com` itself.
3. GC domains, computed live at evaluation time: the distinct domains of every
   `gc_contacts.email`, minus the internal domains, minus the public-mailbox
   provider list (gmail.com, googlemail.com, yahoo.com, outlook.com,
   hotmail.com, live.com, msn.com, aol.com, icloud.com, me.com, mac.com,
   protonmail.com, proton.me, mail.com, ymail.com). A GC contact at a public
   provider still authorizes their exact address.

A match stores `authorization_rule_id` (null for a GC-domain match) and
`authorization_kind` (`address`, `domain`, `gc_domain`). No match stops the
row at `flagged_unauthorized`.

Unauthorized rows appear in the Unauthorized tab with subject, sender,
recipients and body. Actions: "Continue processing" (moves to `method` with
`invitation_method = nonorganic` and records who continued it) or "Dismiss"
(`rejected_by_review`). Adding the sender as a rule from this screen is a
one-click shortcut that opens the settings form pre-filled.

Learn-back: when a rule is added, rows at `flagged_unauthorized` from the last
14 days whose sender now matches are re-run through `authorize` automatically,
mirroring the PM ingestion's rescan on project creation. In the same pass,
rows at `classify` from that period that are waiting (`next_attempt_at` set:
a classify budget deferral, 3.5) and whose sender the new rule covers have
`next_attempt_at` and `last_error` cleared (`release_classify_waits_for_rule`),
so the next tick sweeps them in its first window instead of leaving them to
wait out the budget window. Both passes apply the rule server-side (an
`address` rule by equality, a `domain` rule as `%@domain` and `%.domain`,
then the authorize matcher on each hit) and read the hits in pages
(`_rows_for_rule`), so a flood of other senders' rows cannot push the new
rule's rows past PostgREST's row cap and out of the learn-back.

### 3.8 Invitation method

Decided from what authorized the sender, in this precedence:

1. A locked rule (address rule before domain rule) gives its method: a
   platform (`procore`), `pipelinesuite` for a GC whose plan room is a
   PipelineSuite portal, `smartbid` for ConstructConnect's SmartBid, or
   `gc_portal` for a GC that invites through its own bespoke bidding portal.
2. A GC-domain match gives `organic`.
3. A user-added rule gives `general` (its `method` column is always `general`
   for non-locked rules).
4. A human "continue" on an unauthorized message gives `nonorganic`.

When more than one rule matches (for example a GC domain that a user also added
as a rule), the higher precedence wins. This is verified by a unit test after
the build, and the test result is reported back before release.

The method is stored on the row and can be corrected on the detail screen by
any review-queue role, since the later harvest step depends on it.

`gc_portal` (2026-09-16): a GC with its own bidding portal usually sends from
its own domain, which is also a GC-contact domain, so without a rule the
sender is plain `organic`. The IT Admin adds a locked domain rule on that
domain with method `gc_portal`; precedence 1 then wins over precedence 2 and
the row is labeled `gc_portal`. The rule's domain is what the harvest step
uses to choose that GC's scraper (`RFP_HARVEST.md` 2.3); subdomains are
covered on a label boundary, like every domain rule. A row whose method was
corrected by hand to `gc_portal` resolves the scraper from its From domain
the same way.

### 3.9 PipelineSuite portals (2026-09-16)

`pipelinesuite` is the method for a GC whose plan room is a PipelineSuite
(PreconSuite) portal at `<gc>.pipelinesuite.com`. It works like `gc_portal`
at the authorization layer and unlike it at the harvest layer: one
method-keyed harvester serves every PipelineSuite GC, so the next GC is a
rule, not code.

- Granted by a locked domain rule on the GC's own sending domain (the
  invitation comes from that domain, through SendGrid, not from a platform
  domain). Precedence 1 wins over the GC-domain match exactly as for
  `gc_portal`. Migration 0129 seeds `cgandbinc.com` (CG&B Enterprises) and
  `shfcontracting.com` (SHF International) and widens both check
  constraints; the IT Admin adds the next portal as a locked domain rule
  with method `pipelinesuite`.
- The harvester needs no credentials in the env: it reads the portal host,
  the Project ID and the Security Key from the email body. A direct human
  reply from the same domain gets the method too but carries no portal
  block, so the Project data card says so and the row drains to `done` like
  any organic row.
- Everything past the method (the client, the session per portal host, the
  tracking pings, the harvest data shape, the settings and the tests) is in
  `RFP_PIPELINESUITE.md`; the harvest wiring in `RFP_HARVEST.md` 2.4.

### 3.9.1 SmartBid (2026-10-01)

`smartbid` is the method for invitations sent through ConstructConnect's
SmartBid (sometimes "SmartInsight"). It works like `procore` at the
authorization layer (the platform is the sender) and like `pipelinesuite` at
the harvest layer (no credentials in the env).

- Granted by ONE locked domain rule on the platform domain,
  `smartbidnet.com`, which covers `com2.smartbidnet.com` on a label
  boundary. Migration 0148 seeds it and widens both check constraints. The
  GC is not the sender, so the GC comes from the extract step (the email
  signature), exactly as for Procore.
- The harvester needs nothing from the env: the "Click Here to View the
  Project" link in the email body carries the bid project id and a
  per-recipient passport key. A SmartBid row with no such link (a marketing
  blast, for example) drains to `done` unharvested, like any organic row.
- Everything past the method (the client, the login, the tracking pings, the
  harvest data shape, the settings and the tests) is in `RFP_SMARTBID.md`;
  the harvest wiring in `RFP_HARVEST.md` 2.6.

### 3.10 Done

`method` no longer hands the row to `done`: it hands it to `extract`, then
`match` (`RFP_MATCHING.md` sections 2 and 3), and `done` now means
"processed, no existing project found, parked for the later creation step".
Rows the intake slice parked at `done` before that slice are moved to
`extract` by migration 0122 and by the startup backfill. Nothing in this
slice creates projects, sends email, or downloads files.

---

## 4. Data model

Migration: next free number after the sandbox slice's 0119. Confirm the number
with the sandbox session before applying, since both sessions are adding
migrations. Applied manually to the dev database only, per the standing rule.

`rfp_emails`

| column | notes |
|---|---|
| id | uuid pk |
| internet_message_id | text, unique, normalized |
| primary_mailbox | text |
| conversation_id | text, indexed |
| from_address, from_name | text |
| to_recipients, cc_recipients | jsonb arrays of `{name, address}` |
| subject | text |
| body_preview | text |
| body_text | text, capped |
| received_at | timestamptz, indexed |
| has_attachments | bool |
| attachments_meta | jsonb array of `{name, contentType, size, kind}` |
| auth_spf, auth_dkim, auth_dmarc, auth_compauth | text |
| auth_raw | text |
| auth_verdict | `pass` / `fail` |
| keyword_hits | text[] |
| llm_answer | `yes` / `no` / `undetermined` |
| llm_confidence | numeric(4,3) |
| llm_reasoning | text |
| llm_model, llm_prompt_version | text |
| classified_at | timestamptz (0147): when the classify model answered; what the per-sender classify budget counts by (3.5) |
| review_decision, review_by, review_at | text, uuid, timestamptz |
| authorization_kind, authorization_rule_id | text, uuid |
| continued_by, continued_at | uuid, timestamptz (the unauthorized override) |
| invitation_method | text; check `rfp_emails_invitation_method_check` (0124, widened by 0127, narrowed by 0128, widened by 0129 to organic, procore, pipelinesuite, gc_portal, general, nonorganic, widened by 0148 to add smartbid) |
| status | text, indexed with next_attempt_at |
| flag_reason | text |
| decided_at_step | text |
| attempts | int |
| next_attempt_at | timestamptz |
| last_error | text |
| created_at, updated_at | timestamptz |

`rfp_blocked_senders` (0133)

| column | notes |
|---|---|
| id | uuid pk |
| kind | `address` (one person) / `domain` (everyone at the company) |
| value | text, lowercased and normalized by `validate_block` |
| reason | text, optional note, capped at 200 chars by the API |
| locked | bool; the four platform rows 0133 seeds, removable by an IT Admin or a dev only |
| created_by | uuid, null for the seed |
| created_at | timestamptz |

Unique on `(kind, value)`, which is what makes two people pressing Block at
the same instant a 409 rather than two rows. Deny-by-default RLS like every
other `rfp_*` table. 0133 also adds the terminal status `blocked_sender` to
`rfp_emails_status_check`.

`rfp_email_sightings`: `rfp_email_id`, `mailbox`, `graph_message_id`,
`received_at`, unique (`mailbox`, `graph_message_id`). One row per watched
mailbox that received the message.

`rfp_authorized_senders`: as in 3.7, plus `created_at`, unique (`kind`,
`value`); check `rfp_authorized_senders_method_check` (0124, widened by 0127,
narrowed by 0128, widened by 0129 to procore, pipelinesuite, gc_portal,
general, widened by 0148 to add smartbid).

`rfp_classify_training`: `rfp_email_id`, `subject`, `body_excerpt`,
`llm_answer`, `llm_confidence`, `human_answer`, `decided_by`, `created_at`.

All tables: RLS enabled and forced, deny by default; the service-role backend
is the only reader. PostgREST schema reload after the DDL.

---

## 5. Concurrency and race conditions

- Two production uvicorn workers: one fenced lease with a holder token. Only
  the holder syncs or sweeps. The sweep renews every 20 rows on the free
  steps and before every LLM call (`RFP_MATCHING.md` section 5), and aborts
  if the lease was taken.
- The same message in several mailboxes: `rfp_emails.internet_message_id` is
  unique and the insert is on-conflict-do-nothing, followed by the sighting
  insert, itself on-conflict-do-nothing. Even if two runners overlapped, the
  second insert loses cleanly and only adds a sighting.
- A message re-pulled after a delta reset: same two unique indexes, no
  duplicate rows, no repeated LLM spend because terminal rows are never
  re-run.
- Review versus sweep: every human action is a conditional update on the
  expected status; the sweep never touches `review_llm` or
  `flagged_unauthorized` rows.
- Rule changes mid-flight: the authorize step reads rules at execution time.
  Rules added later trigger the 14-day learn-back over flagged rows.
- LLM spend: a row can only be at `classify` once, and the step writes its
  verdict before advancing, so a crash between call and write costs at most
  one repeated call for that row.
- Sweep order (security review 2026-09-30): the batch is filled in two
  windows. First, pending rows that have never waited (`next_attempt_at`
  null: fresh mail, a row a person released), oldest first. Then, in what is
  left of the 200, rows whose wait elapsed (a retry, a model wait, a classify
  budget deferral), ordered by `next_attempt_at` then `received_at`, so they
  go in the order they became due. Every push re-stamps a row at the back of
  that second window. A flood of deferred rows therefore cannot fill the
  batch ahead of newer mail, and a retry that has waited longest is not
  starved by rows re-deferred after it. The second window keeps a reserved
  slice of the batch (`_SWEEP_WINDOW2_RESERVE`, 40 of the 200) whenever due
  waited rows exist; the first window gets the other 160, or the whole 200
  when nothing is due. The guarantee: N fresh rows drain in ceil(N / 160)
  ticks while waited rows are due (ceil(N / 200) otherwise), and waited rows
  (retries, model waits, split and create polls, budget deferrals) are never
  starved, whatever the inbound rate.

---

## 6. Configuration

| env | default | meaning |
|---|---|---|
| RFP_INGESTION_ENABLED | false | master switch shared with the sandbox slice; the router and the `rfp_email_ingest` features key follow it |
| RFP_EMAIL_INGESTION_INBOXES_ALLOWED | empty | comma-separated mailboxes; empty means the poller never runs |
| RFP_EMAIL_INGESTION_POLL_INTERVAL_SECONDS | 120 | tick interval |
| RFP_EMAIL_INGESTION_LOOKBACK_DAYS | 3 | first-sync window, dev and production alike |
| RFP_EMAIL_INGESTION_RESET_LOOKBACK_DAYS | 7 | window after a 410 delta reset |
| RFP_EMAIL_INGESTION_INTERNAL_DOMAINS | g3electrical.com | senders here are ignored |
| RFP_EMAIL_INGESTION_BLOCKED_DOMAINS | empty | EXTRA domains (with subdomains) sanitized out at listing time, pinned by the environment and not removable from the page. The managed list is the `rfp_blocked_senders` table, which 0133 seeds with the four platform domains this value used to default to |
| RFP_EMAIL_INGESTION_SHARED_MAILBOXES | bids@g3electrical.com | comma-separated mailboxes every internal role may see into. Every other row is visible only to the people its mailbox is mapped onto in `profiles.rfp_mailboxes` (0134, docs/RFP_EMAIL_VISIBILITY.md) |
| RFP_EMAIL_INGESTION_CONFIDENCE_THRESHOLD | 0.85 | classify threshold |
| RFP_EMAIL_INGESTION_CLASSIFY_MAX_BODY_CHARS | 12000 | body characters sent to the model |
| RFP_EMAIL_INGESTION_CLASSIFY_RETRY_SECONDS | 300 | wait when the model is down |
| RFP_EMAIL_INGESTION_CLASSIFY_MAX_ATTEMPTS | 4 | real-failure cap before `failed` (about 21 minutes) |
| RFP_EMAIL_INGESTION_CLASSIFY_BUDGET_PER_SENDER_PER_DAY | 20 | classify calls per budget key (the registrable From domain with its subdomains, or the canonical address at a public mail provider or ISP mailbox) per rolling day for a sender no rule or GC domain authorizes; a public suffix as a whole gets 10 times this; rows over it wait at `classify` (3.5). 0 disables |
| RFP_EMAIL_INGESTION_CLASSIFY_BUDGET_PER_TICK | 25 | classify calls per sweep tick for such senders; the rest wait for a later tick. 0 disables |
| RFP_MODEL_AWAY_ALERT_MINUTES | 60 | model away this long with rows waiting: one IT Admin alert |
| SELF_HOSTED_RFP_CLASSIFY_MODEL | empty | model name on the box |
| OPENAI_RFP_CLASSIFY_MODEL | gpt-5.4-mini | 3rd-party pool model |

Lookback, explained: when the poller starts for the first time on a mailbox it
has no delta link, so it must decide how far back in the Inbox to read. Three
days means anything older than that is never seen. The same window applies on
the production first start. It can be changed before the first start if a
different backfill is wanted; it has no effect afterwards.

Boot validation: each mailbox must look like an address; the poller logs and
skips a mailbox that returns 404 from Graph rather than retrying every tick.

---

## 7. API

All routes require the feature switch and an internal role. The review queue
ACTIONS require a review-queue role (Estimating Admin, IT Admin, Executive,
Estimating Engineer Materials, Estimating Engineer Labor); the four READS
(`GET ""`, `/counts`, `/match-stats`, `/{id}`) take that set plus the
read-only accountant.

Since migration 0134 every read and every action is also scoped to the
MAILBOXES that received the row: see `docs/RFP_EMAIL_VISIBILITY.md` for the
whole rule. In short, a person sees a row whose `mailboxes` overlap the
mailboxes mapped onto their profile (`profiles.rfp_mailboxes`) or the shared
list (`RFP_EMAIL_INGESTION_SHARED_MAILBOXES`); a dev account wearing the IT
Admin role sees everything and also gets `owners` on each list row. A row
outside the viewer's scope answers the same 404 an unknown id does, on every
`/{id}` route, so ids never leak. The counts, the badges and `/match-stats`
count only what the viewer can see.

| method and path | purpose |
|---|---|
| GET /rfp-emails?tab=review\|unauthorized\|flagged\|processed&limit&offset | `{items, total, offset, limit}`, newest first; list rows never carry the body |
| GET /rfp-emails/{id} | the full row plus `mailboxes` (sightings) and `rule` (the matched authorized-sender rule or null) |
| POST /rfp-emails/{id}/review {decision: yes\|no} | LLM review action |
| POST /rfp-emails/{id}/continue | unauthorized override |
| POST /rfp-emails/{id}/dismiss | unauthorized dismiss |
| PATCH /rfp-emails/{id} {invitation_method} | correct the method |
| GET /rfp-emails/counts | badge counts for the sidebar and dashboard |
| GET /rfp-emails/authorized-senders | `{items}`; locked first; each with `created_by_name` |
| POST /rfp-emails/authorized-senders {kind, value, locked?, method?} | 201 `{item, rescanned}`; `locked` and `method` (`procore`, `pipelinesuite`, `smartbid`, `gc_portal`, `general`) accepted from the IT Admin only, every other rule is `general`; runs the 14 day learn-back |
| DELETE /rfp-emails/authorized-senders/{id} | remove; locked rows IT Admin only |
| GET /rfp-emails/blocked-senders | `{items, env_domains}`; locked (platform) first; each with `created_by_name`. `env_domains` is what the server environment still pins, listed read-only |
| POST /rfp-emails/blocked-senders {kind, value, reason?} | 201 `{item, parked}`; blocks and parks everything from that sender still in flight |
| DELETE /rfp-emails/blocked-senders/{id} | unblock; locked (platform) rows IT Admin or dev only |
| POST /rfp-emails/{id}/block-sender {kind, confirm, reason?} | block the open message's sender; the value comes from the stored `from_address` and `confirm` must equal it. `{item, parked, email}` |

The blocked-sender routes take `require_block_admin`: the Executive, the
Estimating Admin and the IT Admin, plus any dev account whatever role it is
currently wearing. Removing a locked (platform) block is the IT Admin's or a
dev's alone, checked per ROW inside the handler.

The detail response never includes attachment content or HTML. Every action
is audited exactly once: the service audits the row actions inside the
conditional update that wins the race (`rfp_email.review`, `.continue`,
`.dismiss`, `.set_method`, `.block_sender`) and the two block writes
(`rfp_blocked_sender.create`, `.delete`); the router audits
`rfp_authorized_sender.create` and `.delete`. Refusals carry an
`X-Error-Code` header: `rfp_rule_locked_it_admin_only` (403),
`rfp_rule_invalid` and `rfp_rule_public_domain` (400, detail is the
user-facing sentence), `rfp_rule_duplicate` (409),
`rfp_email_not_actionable` (409, someone else acted first), and for blocks
`rfp_block_locked_it_admin_only` (403), `rfp_block_invalid`,
`rfp_block_public_domain`, `rfp_block_internal_domain` and
`rfp_block_confirm_mismatch` (400), `rfp_block_duplicate` (409).
`POST /gc-contacts` gained `rfp_notice`, non-null when the contact's email is
at a public mailbox provider.

---

## 8. Frontend

- `/rfp-emails` page (Bidding nav, behind the features key). Tabs: Review,
  Unauthorized, Flagged, Processed. Each row: received time, sender, subject,
  keyword hits, model answer with confidence, status. Badge counts on the nav
  item for Review plus Unauthorized.
- Detail drawer: metadata block, auth verdict block, model block, plain-text
  body rendered in a monospace block with links displayed as text (no anchors,
  no images), attachment list without links, action buttons per status.
- Settings, "RFP Ingestion" tab, visible to Executive, Estimating Admin and
  IT Admin: rules table with kind, value, method, locked badge, added by; add
  form with a kind selector (address or domain) and value; remove button
  disabled on locked rows unless the viewer is the IT Admin; a "locked"
  checkbox on the add form for the IT Admin only, and once it is ticked a
  method picker (GC portal, PipelineSuite, Procore, SmartBid, General; default
  General) so the locked rule names the source it stands for.
- Settings, "Blocked senders" section on the same tab (2026-09-22), visible
  to those three roles plus any dev account: the blocked table with scope
  (One sender / Whole company), value, reason, blocked by and an Unblock
  button, disabled on the built-in platform rows unless the viewer is an IT
  Admin or a dev; an add form with the same scope selector, the value and an
  optional reason. Domains the environment pins are listed under the table,
  read-only, with the env var named.
- Review queue detail: a "Block sender" button for those same roles on any
  message not already blocked. It opens a confirm dialog that states what
  blocking does, offers the two scopes with their consequences spelled out
  ("This sender only" versus "Everyone at this company", the domain shown),
  and requires the exact value to be typed before the button turns on. After
  it runs the dialog's parent shows what was blocked and how many queued
  messages were ended.
- Dashboard: a task card for the Estimating Admin when Review or Unauthorized
  is non-empty.
- Notifications: one bell row per tick that adds new Review, Unauthorized or
  Matches rows, deduped, never one per message. Since 0134 it is addressed by
  mailbox (docs/RFP_EMAIL_VISIBILITY.md 3.4): rows in a shared mailbox go to
  the Estimating Admin role as before, rows in a person's own mailbox go to
  that person.
- i18n catalogs under `bdr_fe/locales` for every string. No em dashes.

---

## 9. Security notes

- Mail content is attacker-controlled. The body is stored and shown as plain
  text; HTML is never fetched. No attachment bytes, no link resolution.
- Authorization is decided on the authenticated From address; the auth step
  runs first so a spoofed From cannot become authorized.
- The model sees the body as delimited untrusted text with an explicit
  instruction to ignore embedded instructions. The delimiter strings are
  scrubbed from every outside-written field in the block (body, subject,
  and the sender's display name and address) so none can close it early.
  Its output is schema-checked and clamped, and only ever routes a row
  between states. The match prompt (docs/RFP_MATCHING.md) treats the
  candidate projects' names, numbers, GC names and bid notes as untrusted
  data too, scrubbed the same way: for a project created from an earlier
  invitation those fields were copied from outside mail or a portal.
- Classify spend is budgeted per sender (the registrable domain, the
  canonical mailbox at a public provider, and a public suffix as a whole)
  and per tick for senders no rule or GC domain authorizes (3.5), counted
  by classification time, so a flood from throwaway domains, sibling
  subdomains or plus-tagged free accounts cannot burn the model budget;
  the remaining gap is a real registrable domain owned by the attacker
  (one per-key budget each, so spend scales with domains bought); and the sweep
  serves rows that never waited before deferred rows (section 5), so the
  flood cannot hold real invitations behind it either. Its rows drain at
  the per-tick cap after everything else.
- Rules are lowercased and validated (address must parse, domain must be a
  bare hostname). A domain rule for a public mailbox provider is refused with
  a descriptive message telling the user to add full addresses instead, and
  saving a GC contact at such a provider returns a notice that the contact is
  matched by full address only and every colleague who might send invitations
  should be added. Locked rules are protected server-side by role, not only in
  the UI.
- Rate limits: the review and settings routes use the default bucket; the
  poller is not user-triggered.

---

## 10. Testing plan

- Unit: Authentication-Results parser across pass, fail, none, softfail,
  missing header, forged non-tenant header. Keyword regex boundaries
  (`feedback` must not hit `db`, `Steve` must not hit `ve`). Authorization
  precedence matrix including a GC domain that is also a user rule and a GC
  contact at gmail.com. Message-ID normalization.
- Pipeline: crash-resume at every step, sweep with the model down (no attempt
  spent, rows wait), backlog drain when it returns, conditional review
  updates under a simulated double click.
- E2E on dev: send real messages to a watched mailbox from an external
  address, a GC-domain address, a platform address and an internal address,
  and walk each through the UI. Confirm the RFQ-thread skip against a live
  RFQ reply in the bids mailbox.
- Report the precedence test outcome and any surprises before release.

---

## 11. Build record (2026-09-10)

Backend: `app/services/rfp_email_auth.py` (pure: header parser, keyword gate,
public-provider list, rule validation, authorization evaluator),
`app/services/rfp_email_ingest.py` (poller, sweep, steps, human actions,
learn-back), `app/routers/rfp_emails.py`, `rfp_notice` on `POST
/gc-contacts` in `app/routers/reference.py`, feature `rfp_classify` in
`app/services/llm.py` and `llm_health.FEATURE_LABELS`, `rfp_email_ingest` in
`GET /features`, poller and router wired in `app/main.py`. Settings in
`app/core/config.py` under the `rfp_email_ingestion_` prefix;
`rfp_ingest_enabled` now reads `RFP_INGESTION_ENABLED` (alias
`RFP_INGEST_ENABLED`) and the settings model is `populate_by_name`.
Tests: `tests/test_rfp_email_auth.py`, `tests/test_rfp_email_ingest.py`,
`tests/test_rfp_emails_router.py` (146 tests); the full suite passes apart
from two pre-existing `test_llm_queue` sandbox-row failures owned by the
sandbox slice.

Frontend: `app/(app)/rfp-emails/page.tsx` (queue + detail modal),
`components/RfpEmailsActivity.tsx` (30 s counts poll, review roles only),
`components/RfpEmailsTaskCard.tsx` (dashboard), `components/
RfpAuthorizedSendersSection.tsx` on `app/(app)/settings/rfp-ingestion/
page.tsx`, sidebar item with count pill, bell routing for
`rfp_email.review`, public-provider hint and `rfp_notice` on the GC contact
forms, `lib/rfpEmails.ts` (roles, vocab, types), keys in all six catalogs.

Deviations from the design, all deliberate:
- Domain rules cover subdomains (section 3.7), after the live run.
- Auth tokens are stored by the fetch step (the headers arrive with that
  GET); the auth step judges the stored columns, so a crash between the two
  needs no re-fetch. `flag_reason` distinguishes `dmarc_fail`, `all_failed`
  and `no_tenant_auth_header`.
- Unusable model output: the first spends one attempt with backoff, the
  second lands the row in review as `undetermined` rather than burning the
  cap. `not_configured`, `out_of_tokens` and `unauthorized` provider errors
  wait like an outage (operator problems are fixed in the env, not per row).
- The method step re-derives from the stored rule; if the rule was deleted
  in between, the row bounces to `flagged_unauthorized` with
  `rule_removed`.
- `complete_json` has no temperature parameter; the answer is a schema
  enum, which is what routing depends on.

Addendum 2026-09-14: vendor-sender listing skip (`VendorSenders`,
`vendor_senders` in `rfp_email_ingest.py`, applied in `_insert_from_delta`
next to the internal skip) and classify prompt `rfp_classify_v2` (section
3.5). Rows already stored before the change keep `rfp_classify_v1` and are
not re-judged. Tests: `test_vendor_senders_shape`,
`test_delta_skips_vendor_senders`,
`test_delta_vendor_skip_is_optional_for_direct_callers`.

Addendum 2026-09-15: BuildingConnected and NGEM dropped as invitation
methods. `should_skip_sender` takes `blocked_domains`
(`RFP_EMAIL_INGESTION_BLOCKED_DOMAINS`, subdomains covered by
`domain_covered_by`), wired through `_insert_from_delta` from the sync loop;
`INVITATION_METHODS` and `RULE_METHODS` lost the two values in the service
and the router; the extract prompt is `rfp_extract_v2` (platform list without
the two names). Migration 0124 deletes the rows from those senders and the
two seed rules and replaces the unnamed 0120 checks with
`rfp_emails_invitation_method_check` and
`rfp_authorized_senders_method_check`. Tests:
`test_should_skip_sender_blocks_platform_domains_and_subdomains`,
`test_blocked_domain_setting_parses_and_normalizes`,
`test_delta_skips_blocked_platform_senders`,
`test_migration_0124_drops_the_two_platforms_and_names_both_constraints`.

Addendum 2026-09-16: PlanHub dropped as an invitation method the same way.
`RFP_EMAIL_INGESTION_BLOCKED_DOMAINS` now defaults to
`buildingconnected.com,ionwave.net,planhub.com,planhubprojects.com`
(`planhub.com` covers the seed's `message.planhub.com`;
`planhubprojects.com` is the second domain PlanHub sends from, seen live on
dev). `INVITATION_METHODS` and `RULE_METHODS` lost `planhub` in the service,
the router and the FE (`lib/rfpEmails.ts`, six catalogs); the extract prompt
is `rfp_extract_v3` (platform list without PlanHub). Migration 0128 deletes
the rows from those senders (and any row hand-labelled `planhub`), the
PlanHub seed rule, and re-adds both named checks with the narrowed
vocabulary. Procore is the only seeded platform rule left. Test:
`test_migration_0128_drops_planhub_and_narrows_both_constraints`, plus the
PlanHub cases added to the blocked-domain tests above.

Addendum 2026-09-16 (later the same day): `pipelinesuite` added as an
invitation method (section 3.9, `RFP_PIPELINESUITE.md`). Migration 0129
widens both named checks by that one value and seeds two locked domain
rules, `cgandbinc.com` and `shfcontracting.com`, with an `on conflict
(kind, value) do update` so a hand-added rule on either domain is upgraded
in place. `INVITATION_METHODS` and `RULE_METHODS` gained `pipelinesuite` in
the service, the router (`MethodPatchIn`, `AuthorizedSenderIn`) and the FE.
The migration touches no email rows: after applying it, rows from the two
domains parked at `flagged_unauthorized` are re-authorized by running
`rescan_after_rule_added` once per seeded rule (what the POST
authorized-senders route does). Tests:
`test_migration_0129_adds_pipelinesuite_and_seeds_two_locked_rules`,
`test_the_it_admin_may_add_a_locked_pipelinesuite_rule_on_a_gc_domain`,
`test_locked_pipelinesuite_rule_outranks_the_same_gcs_organic_match`.

Addendum 2026-10-01: `smartbid` added as an invitation method (section 3.9.1,
`RFP_SMARTBID.md`). Migration 0148 widens both named checks by that one value
and seeds one locked domain rule, `smartbidnet.com`, with an `on conflict
(kind, value) do update` so a hand-added rule on that domain is upgraded in
place. `INVITATION_METHODS` and `RULE_METHODS` gained `smartbid` in the
service, the router (`MethodPatchIn`, `AuthorizedSenderIn`) and the FE. The
migration touches no email rows: after applying it, rows from
`smartbidnet.com` parked at `flagged_unauthorized` are re-authorized by
running `rescan_after_rule_added` once for the seeded rule, which is a
release step that needs the owner's go (on dev it touches about 55 rows
across about 12 real projects, and every new project the match step does not
find gets logged into and downloaded). Tests:
`test_migration_0148_adds_smartbid_and_seeds_one_locked_rule`,
`test_the_it_admin_may_add_a_locked_smartbid_rule_on_the_platform_domain`,
`test_locked_smartbid_rule_outranks_the_platform_senders_other_matches`.

## 12. First live run (2026-09-10, dev database, real mailboxes)

Three-day window across the four mailboxes, model box up. 738 rows: 279
`flagged_llm_no`, 186 `flagged_no_keywords`, 117 `done` (now 131 after the
subdomain fix), 66 `flagged_unauthorized` (now 52), 21 `review_llm`, 15
`flagged_auth`, the rest still draining. The bids mailbox contributed one
row, so the RFQ-thread skip works. 180 messages were seen in more than one
mailbox and became one row each.

What it showed, and what was done or is still open:

- Procore sends from `us02.procoretech.com`, not the bare domain. Fixed by
  the subdomain rule; the 14 affected rows were rescanned to `done/procore`.
- SmartBid (`com2.smartbidnet.com`, 12 rows) is a real invitation platform
  with no seed rule. RESOLVED 2026-10-01: migration 0148 adds `smartbid` as
  an invitation method and seeds `smartbidnet.com` as a locked rule (section
  3.9.1, `RFP_SMARTBID.md`).
- `eoc-gc.com` (16 rows) and other GC domains are unauthorized on dev only
  because the dev database has almost no GC contacts; production will
  authorize them organically.
- Per-recipient duplicates: platforms (Procore, ConstructConnect, and NGEM
  before it was blocked) send one message per recipient with distinct
  Message-IDs, so the cross-mailbox dedup cannot merge them. 121 groups,
  245 extra rows. Open: this is the match step's job (same sender plus
  normalized subject in a window), not this slice's.
- `no_tenant_auth_header` (9 rows) were Microsoft quarantine digests and
  internally forwarded meeting items whose From is the outside organizer.
  Nothing lost; forwarded items are out of scope by decision.
- NGEM "Bid Addendum Notification" mail was classified inconsistently
  (`no` at 0.99 on some, `undetermined` at 0.85 to 0.95 on others, which
  landed in review). Moot since 2026-09-15: NGEM mail is dropped at listing
  time and never reaches classify. The general point (the prompt could name
  addenda and question answers as `no`) still applies to other senders and
  is left for tuning once review decisions accumulate in
  `rfp_classify_training`.
- Real invitations ("Reminder to submit your Bid", and at the time the NGEM
  "Bid Opportunity Invitation" and BuildingConnected invites, both since
  removed by 0124) reached `done` with the right method.

## 13. Out of scope for this sprint

- Forwarded invites from internal senders.
- Any human override of `flagged_auth`.
- The harvest step. The match step (extract, match, merge, unmerge) is the
  next slice: `RFP_MATCHING.md`.
- Cloud fallback for `rfp_classify` while the box is off.
- Production migration and Railway variables (needs explicit approval).
