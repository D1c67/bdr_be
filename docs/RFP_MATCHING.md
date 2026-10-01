# RFP Ingestion: Field Extract and Project Matching

Design record for the third slice of RFP Ingestion: after the invitation
method is labeled, pull the project facts out of the email, decide whether
the project already exists in the system, and when it does, attach the
inviting GC to it instead of processing the email any further. It also adds
the same matcher to the New Bid form so a person creating a project by hand
is warned when a similar one exists.

Status: design v2, 2026-09-11. v2 folds in a five-lens adversarial review of
v1 (correctness, security, codebase fit, product fit, operability; 61
confirmed findings). Builds on the email intake slice
(`RFP_EMAIL_INGESTION.md`, migration 0120, dev only). Nothing here creates
projects, sends mail to GCs, or downloads attachments. Dev database only.

Naming, used everywhere: setting prefix `rfp_match_` (env prefix
`RFP_MATCH_`), pure module `app/services/rfp_match.py`, pipeline steps and
human actions in `app/services/rfp_email_ingest.py`, migration
`0122_rfp_matching.sql`, table `rfp_project_matches`, LLM features
`rfp_extract` and `rfp_match`, statuses `extract`, `match`, `review_match`,
`merged`, `duplicate`, FE tab "Matches" on `/rfp-emails`, project modal
"Merged by System", New Bid modal "Similar projects".

Refactors in this slice, all in the backend:

- `proposal_send.remove_gc_link(project_id, gc_id, *, link_id=None,
  refuse_if_sent=False, via_unmerge=False) -> bool`, extracted from the body
  of `DELETE /projects/{id}/gcs/{gc_id}`. Section 3.7.
- `app/core/roles.py` gains `RFP_REVIEW_ROLES`, the tuple that is
  `REVIEW_QUEUE_ROLES` in `routers/rfp_emails.py` today (same five roles);
  the router keeps `REVIEW_QUEUE_ROLES = RFP_REVIEW_ROLES` so
  `require_review_queue` and the existing tests are unchanged, and
  `routers/projects.py` imports `RFP_REVIEW_ROLES` from `app.core.roles` to
  gate the email block of the project-side match route without a
  router-to-router import.
- The sweep lease is renewed before every LLM call and its length becomes a
  setting. Section 5.
- `attempts`, `last_error` and `next_attempt_at` are reset by every write that
  moves a row onto a new pending step, so each step's cap counts its own
  calls. Section 2.

---

## 1. Decisions locked in (2026-09-11)

| Topic | Decision |
|---|---|
| Where it runs | Two new pipeline steps after `method`: `extract` then `match`. `done` keeps its meaning: processed, no existing project found, parked for the later creation step. |
| Extraction | A small schema-enforced LLM extract of four facts: project name, GC name, bid due date and time, bid notes. Nulls allowed. Same wait-while-the-box-is-off behavior as classify. The full document harvest is a later slice. |
| Per-recipient siblings | Platforms send one message per recipient with distinct Message-IDs (113 of the 181 rows this slice backfills). A deterministic sibling check at the top of `extract` reuses a copy's facts and decision instead of spending two LLM calls per copy. The window is symmetric (older AND younger), because the order the copies arrive out of Graph's delta is not ours to choose. Section 3.1. |
| Signals scored | Project name (required signal, highest weight), bid due date, bid notes (boost only). Invitation date and address are NOT used. |
| Required name | An email with no usable extracted project name is never scored against any project; it lands at `done` with `flag_reason = no_project_name`. |
| Date column | The email's due date is compared against `projects.actual_bid_at` first, `internal_bid_at` when the actual is null, and every existing GC's `project_gcs.needs_by`. The closest wins. |
| Date tolerance | Up to `RFP_MATCH_BID_DATE_TOLERANCE_DAYS` (3) apart scores 1.0, the same as an identical date; only the exact-time bonus ranks above it. Inside the band the date can raise a total, never lower it below the no-date total. Farther apart scores down; beyond the candidate window the project is not a candidate. |
| Missing signals | A missing due date never penalizes: its weight is dropped from the denominator. Bid notes are boost-only: shared notes raise the total, different notes (two GCs' own instructions for the same job) change nothing. A name-only match must clear a higher name floor to auto-merge. |
| Bid notes | New `projects.bid_notes` column, separate from `projects.notes`, on intake and project details. The extracted notes are stored on the email and shown in the merge record; they are never written into the project automatically. |
| Name matching | Layered: deterministic trigram plus token scoring in Python, reference numbers stripped first, a hard cap for conflicting discriminator tokens (Phase 1 vs Phase 2), then an LLM verdict per candidate. Auto-merge requires the score AND a confident LLM "same". Two candidates too close to call (within `RFP_MATCH_RUNNER_UP_GAP` after the confident-different filter) always go to review; the system never picks between equals. No auto-merge ever happens on the deterministic score alone. |
| Candidate window | Only projects whose bid date (`actual_bid_at`, else `internal_bid_at`) is not more than `RFP_MATCH_CANDIDATE_WINDOW_DAYS` (30) in the past. Projects with neither date, abandoned, declined, PM-only and CP-only projects are never candidates. Won, lost and no-award projects are candidates only inside that window. |
| Same GC already on the project | Terminal status `duplicate`. Nothing is added, nothing on the project changes, the record links the email to the project and GC. A wrong duplicate is reversed with `reopen` (3.8). A per-recipient sibling (3.1) of a row a person or the system already merged or marked duplicate follows the leader's decision without the switch or the sender gate, because it is the same message. |
| GC resolution | Organic senders resolve by contact email address, then contact domain. Everything else resolves by fuzzy name against `general_contractors`. Below the GC threshold, or ambiguous, the email waits in the Matches tab where a person picks or adds the GC. |
| Verified sender | The system merges or marks a duplicate automatically only when the tenant header shows an aligned pass (`auth_dmarc = pass` or `auth_compauth = pass`) and `authorization_kind` is `address`, `domain` or `gc_domain`. A human-continued (override) row always waits for a person. |
| Rebids | Manual `projects.is_rebid` flag on intake and project details. A wider name-only lookup (`RFP_MATCH_REBID_LOOKBACK_DAYS`, 365, own floor `RFP_MATCH_REBID_NAME_THRESHOLD`) never merges; it records `possible_rebid_project_id` and score on the email and runs inside the New Bid check too, so the person creating the project by hand is told before creating it. |
| Post-send merges | Stage never moves. A project whose bid has gone out (`proposal_send.bid_has_gone_out`) shows a "New RFP" pill and returns to the dashboard's active list while a system-merged GC has no sent or sending proposal, is not acknowledged, and is still on the project. The existing per-GC "Send proposal" flow (0104/0118) sends it. The pill clears when the proposal is sent or marked submitted, when the row is acknowledged ("No proposal needed"), or on unmerge. Derived, never stored. |
| Merged by System | Every merge and duplicate is a row in `rfp_project_matches`. Only open merges drive the "Merged by System" count; unmerged merges and duplicates stay as history. Any row makes the project's RFP matches modal reachable. Unmerge closes a row, it never deletes it. |
| Unmerge | Estimating Admin, Executive and IT Admin, with a required reason. Refused once a proposal has been sent (or is sending) to that GC. Removes exactly the link the system added (by link id, never one a person added), records who and why, adds the project to the email's `excluded_project_ids` and returns the email to `match` so the sweep re-runs it against everything else. Cascades to the sibling duplicates that only existed because of this merge. The ordinary GC panel Remove is refused for a system-added GC; unmerge is the only detach path. |
| Auto-merge switch | `RFP_MATCH_AUTO_MERGE_ENABLED` (default false). While off, every confident match and duplicate still lands in the Matches tab for one human click, labeled as confident, and the Matches tab reports how often reviewers agreed with the system so the switch can be turned on with evidence. |
| New Bid check | `POST /projects/similar {name}` runs the deterministic name scorer (no LLM, no dates) at a lower threshold over a wider window on `internal_bid_at`, plus the rebid lookup. The New Bid form calls it before creating; a modal lists similar projects and possible rebids with links and offers Cancel, Create anyway, or Create as a rebid. Advisory: it fails open. |
| Weights in env | Every weight, threshold, tolerance and window is an env value. Each decision stores the full resolved `rfp_match_*` settings plus `SCORER_VERSION` (a constant in `rfp_match.py`, bumped when a stop token, discriminator word, the containment rule, the date ladder or the aggregation changes), so tuning never makes an old decision unexplainable. |
| Backfill | Rows parked at `done` by the intake slice with no `extracted_at` are moved to `extract` by the migration, and the new backend repeats the same guarded move once at startup. Idempotent. |

---

## 2. Pipeline overview

```
... -> method
        |
        v  extract    0. sibling check (same sender, subject, attachments, same GC
        |                identity, within minutes either way): follow the earliest
        |                DECIDED copy (resolved to its chain root), else wait
        |                behind an older undecided one, no LLM
        |             1. LLM: project_name, gc_name, bid_due, bid_notes (nulls allowed)
        |                box off -> wait, no attempt spent (as classify)
        v  match      0. the same sibling check, DECIDED copies only, never a wait
        |             1. resolve the GC (contact, domain, then fuzzy name)
        |             2. no usable name -> done (no_project_name)
        |             3. candidates: projects in the window, scored in Python
        |             4. LLM verdict per candidate above the review threshold
        |             5. route:
        |                  best and runner-up within the gap                  -> review_match
        |                  confident + verified sender + GC resolved
        |                    + GC already on project + auto-merge on         -> duplicate
        |                  confident + verified sender + GC resolved
        |                    + auto-merge on                                  -> merged
        |                  confident but auto-merge off, sender unverified,
        |                    GC unresolved, middle band, or unusable output   -> review_match
        |                  nothing above the review threshold, or every
        |                    candidate confidently different                  -> done
        v
   review_match  (human: merge into X / already on X / not a match / pick or add the GC)
   merged        (terminal; unmerge returns the row to `match` with the project excluded)
   duplicate     (terminal; reopen returns it to review_match)
   done          (terminal here; reopen returns it to review_match; the creation step attaches later)
```

Status vocabulary after this slice:

- Pending (the sweep picks these up): `received`, `auth`, `keywords`,
  `classify`, `authorize`, `method`, `extract`, `match`.
- Waiting on a human: `review_llm`, `flagged_unauthorized`, `review_match`.
- Terminal: `done`, `merged`, `duplicate`, `flagged_auth`,
  `flagged_no_keywords`, `flagged_llm_no`, `rejected_by_review`, `failed`.

Code constants: `STATUS_PENDING` gains `extract` and `match` (the sweep query
and the `_process_email` loop key on it); `STATUS_HUMAN` gains
`review_match` (badge counts); `STATUS_TERMINAL` gains `merged` and
`duplicate`. Two existing guards stop keying off those tuples:

- `dismiss` accepts exactly `(review_llm, flagged_unauthorized)`; on a
  `review_match` row it is 409 `rfp_email_not_actionable`. The only
  rejection exit from `review_match` is `match/reject` (3.8).
- `set_method` refuses only rows the method step has not yet passed
  (`received, auth, keywords, classify, authorize, method`); every later
  status, pending or not, accepts the correction (no step after `method`
  writes `invitation_method`, and every pipeline write is field-scoped).

Per-step attempts: every CAS that moves a row onto a new pending status
writes `attempts = 0, last_error = null, next_attempt_at = null`
(`received -> auth` is already free; add it to `keywords -> classify`,
`classify -> authorize`, `review_llm -> authorize`, `flagged_unauthorized ->
extract`, `method -> extract`, `extract -> match`, and unmerge `merged ->
match`). The cap `RFP_EMAIL_INGESTION_CLASSIFY_MAX_ATTEMPTS` and the
"unusable output twice" rule then count only the current step's calls. No
new columns. The one exception is the unusable-output exit from `extract`,
which resets `attempts` and `next_attempt_at` but keeps `last_error`, so the
match step and the drawer can tell a model failure from a nameless email;
the match step's no-name `_terminal` write keeps `last_error` too
(`_terminal` writes only status, flag_reason, decided_at_step and
next_attempt_at).

LLM gate per feature: `_sweep` keeps a set of feature keys found unavailable
this tick. `_process_email` returns the feature key (`rfp_classify`,
`rfp_extract`, `rfp_match`) instead of the bare `llm_down`, and skips a row
only at a step whose feature is in the set. A health-snapshot
`model_missing` or `unconfigured` adds only that feature; a `provider_down`
snapshot, a connection error or a timeout on a live call adds every feature
that `llm.resolve` routes to the same provider (all three under
`FULL_SELF_HOSTED_LLMS_ENABLED`). Concretely, `model_unavailable(snapshot,
feature)` returns `(state, detail) | None` (state is the snapshot's
per-feature `state`); `_process_email` returns `(feature_key, scope)` where
scope is `provider` for `provider_down`, a connection error or a timeout,
and `feature` for `model_missing` or `unconfigured`; `_sweep` adds the key
for `feature` scope and, for `provider` scope, every key in (`rfp_classify`,
`rfp_extract`, `rfp_match`) whose `llm.resolve(key, settings).provider`
equals the failing feature's provider. `_run_from` (the inline path behind
`review` and the learn-back `rescan_after_rule_added`) passes the full set
and stops at `extract`, so no LLM step ever rides a request;
`continue_unauthorized` changes its CAS target from `done` to `extract`
(method already `nonorganic`, attempts reset), never calls `_run_from`, and
the row waits for the next tick (its sibling check runs there).
`_wait_for_model` (which receives the detail from that tuple), the backoff
write and the terminal write take the step's expected status instead of the
literal `classify`.

`_SWEEP_SELECT` gains `received_at, attachments_meta, auth_compauth,
invitation_method, extracted_project_name, extracted_gc_name,
extracted_bid_due_at, extracted_bid_due_has_time, extracted_bid_notes,
extracted_at, resolved_gc_id, resolved_gc_contact_id, gc_match_kind,
gc_match_score, excluded_project_ids, sibling_of_email_id` so
`_step_extract`, `_step_match` and `merge_email(bundle=...)` work from the
sweep row; the sibling leader lookup and the human actions re-read through
`_get`.

---

## 3. Step by step

### 3.1 Extract

Sibling check, first and free. Key: same `lower(from_address)`, same
normalized subject (leading re/fw/fwd stripped, whitespace collapsed,
casefolded), and equal `attachments_meta` names and sizes. Body equality is
not part of the key (bodies carry the recipient name). A candidate copy must
also share the GC identity: the same `authorization_kind` and, when both
rows carry one, the same `authorization_rule_id`.

The window is SYMMETRIC: `received_at` within
`RFP_MATCH_SIBLING_WINDOW_MINUTES` (10; 0 disables) in EITHER direction
(`rfp_match.read_siblings` + `sibling_candidates`). The tick is serialized by
one lease, but the order two copies are pulled out of their mailboxes is
Graph's delta's, not ours: a younger copy can be listed and swept a tick
before the older one, and the older one must still find it instead of paying
for a second extract and possibly a second project.

The leader is chosen by `rfp_match.choose_sibling_leader`, a pure function,
in this order (`received_order` = received instant, then `created_at`, then
`id`: total and stable, so two copies stamped at the same instant agree on
one leader from both sides):

1. any DECIDED copy (`done`, `created`, `merged`, `duplicate`), older or
   younger, whichever of them has the earliest `received_order`. `created`
   with a null `created_project_id` is NOT decided: there is no project to
   join, so it is skipped;
2. else the copy with the earliest `received_order` that is still undecided
   (a pending step or a human lane) AND older than this row;
3. else none: this row is the leader and does the work.

Waiting only ever points backwards (rule 2), so two copies can never wait on
each other. A failed, rejected or flagged copy is never followed and never
waited on.

The chosen copy is then resolved to the ROOT of its own chain: it is re-read
in full and `sibling_of_email_id` is followed while it is set (bounded at 8
hops, cycle-safe). Without that, a copy that is itself a follower can be the
earliest decided candidate and a third copy would follow a follower; the
creation step links one generation from the row it creates from, so the far
end of the chain would be stranded with `flag_reason = sibling` and no
project. With it, every copy of one message points at the same root whatever
order the copies arrive in. (`link_harvest_mates` walks the chain
transitively as well, so chains written before this rule still link.)

`received_order` reads `created_at`, so every select that feeds the rule must
carry it: `_SWEEP_SELECT` and `rfp_match.SIBLING_SELECT` both do. Without it
a swept row ranks itself with an empty `created_at`, out-ranks its own copy,
and both copies of a same-instant pair lead.

- Leader at `merged` or `duplicate`: the follower copies the leader's
  `extracted_*` fields, `resolved_gc_id`, `resolved_gc_contact_id`,
  `match_project_id` and `match_score`, sets `gc_match_kind = sibling` and
  `sibling_of_email_id`, stamps `extracted_at`, `matched_at`,
  `decided_at_step = match`, `flag_reason = null`, writes an
  `rfp_project_matches` row `kind = duplicate` for the same project and GC
  with `decided_by = null` and `sibling_of_email_id` set, and CASes
  `extract -> duplicate`. No extract call, no match call, no notification.
  This follows the leader's decision without the auto-merge switch or the
  verified-sender gate because it is the same message; the follower must
  therefore also share the leader's GC identity, which is why a copy with a
  different `authorization_kind` or `authorization_rule_id` is not a
  candidate at all. Two things on the follower are NOT overwritten: a
  project in its own `excluded_project_ids` is never followed onto (an
  unmerge put it there precisely so this row never lands there again, 3.8;
  the row matches normally instead), and a GC a person picked
  (`gc_match_kind = human` with a `resolved_gc_id`) is kept, with the
  project still coming from the leader, exactly as a re-run keeps it (3.2).
- Leader at `created`: the same, plus `created_project_id` copied from the
  leader, so the copy never re-matches against the project its own original
  just created (docs/RFP_CREATE.md 4.5 step 7). The project is re-read first
  (`_joinable_project`, the three checks `rfp_create._project_joinable`
  makes: not excluded, still present, not abandoned) because
  `sibling_decided` is pure and would call a leader sitting on a project
  abandoned or deleted since a decided copy; such a leader is treated as
  undecided and this row extracts and matches normally.
- Leader at `done`: the follower copies the facts, sets
  `sibling_of_email_id`, stamps `extracted_at`, `decided_at_step = match`,
  `flag_reason = sibling`, and CASes `extract -> done`. The creation slice
  collapses siblings by that column, in either direction: it links every row
  whose `sibling_of_email_id` points at the row being created, whether that
  follower is older or younger.
- Leader still undecided and OLDER (a pending status or a human lane:
  `review_llm`, `flagged_unauthorized`, `review_match`): the follower waits
  like the box-off case (`next_attempt_at` pushed by
  `RFP_EMAIL_INGESTION_CLASSIFY_RETRY_SECONDS`, no attempt spent). A younger
  undecided copy is never waited on.
- A copy whose `authorization_kind` differs (an override copy of a verified
  message), or whose `authorization_rule_id` differs when both carry one: no
  short-circuit; this row extracts normally and walks the steps, where the
  verified-sender gate parks it for a person. It never inherits the other
  copy's decision, and it does not wait on it.
- Copies at `failed`, `rejected_by_review`, `flagged_auth`,
  `flagged_no_keywords` or `flagged_llm_no`: no short-circuit; extract
  normally.
- The leader is re-read in full between the pick and the follow. A copy that
  was decided when it was picked and has since been reopened is only waited
  on when it is older than this row; otherwise this row extracts normally
  (the wait must never point forwards).
- A CAS miss on the follow (the row moved under us) is not a follow: nothing
  was written, the step reports None and the row is picked up again on the
  next tick. A `merged`/`duplicate` follow deletes the `rfp_project_matches`
  row it had already inserted.

The same check runs again at the top of `match`, for DECIDED copies only
(`_sibling_short_circuit_match`). A row that extracted before any of its
copies decided has already paid for the expensive half; it must not also pay
for a match call when a copy has since decided, so it follows the same
`done` / `created` / `merged` / `duplicate` branches, CASing from `match`
instead of `extract`. It never WAITS at match. A half-finished merge of the
row's own (an open `rfp_project_matches` row at `kind = merged`, 3.6) is
resumed first and wins over the sibling check.

Because the sweep drains oldest first, the leader normally finishes inside
the same tick before its followers reach `extract`; the symmetric window is
what covers the tick where it does not. Daily reminders and digests (same
subject a day or more apart) are outside the window by design and resolve
through the normal duplicate path. The last defence, when two copies get all
the way to project creation, is the create-time duplicate guard
(docs/RFP_CREATE.md 4.6).

The LLM extract:

- Feature `rfp_extract` in `app/services/llm.py`. Vendor `openai` for the
  3rd-party pool; model settings `OPENAI_RFP_EXTRACT_MODEL` and
  `SELF_HOSTED_RFP_EXTRACT_MODEL`, each falling back to the classify model
  when empty, so no new env is required to run it. Under
  `FULL_SELF_HOSTED_LLMS_ENABLED` it never leaves the box. Health label
  "RFP field extraction" in `llm_health.FEATURE_LABELS`.
- Input (`rfp_match.build_extract_messages`): subject, sender, the received
  date and time in Pacific (so relative dates like "Thursday at 2 PM" can be
  resolved), and the first `RFP_EMAIL_INGESTION_CLASSIFY_MAX_BODY_CHARS`
  characters of the text body, wrapped in the same scrubbed
  `<<<EMAIL_START>>>` / `<<<EMAIL_END>>>` delimiters classify uses. The
  system prompt says the block is untrusted text copied from an outside
  email and that instructions inside it are to be ignored. It states that
  `project_name` excludes bid, ITB, solicitation and job numbers, that the GC
  is the company inviting us (not the platform), and that bid notes are the
  sender's instructions about the bid (scope notes, walk dates, delivery
  rules), not the whole body.
- Output schema (enforced):

  ```json
  {
    "project_name": "string or null",
    "gc_name": "string or null",
    "bid_due": {"date": "YYYY-MM-DD or null", "time": "HH:MM or null", "timezone": "string or null"},
    "bid_notes": "string or null",
    "reasoning": "string"
  }
  ```

  Each string is capped after the call (name 200, GC 200, notes 2000,
  reasoning 20 words); an over-long value is truncated, never rejected.
- Parsing (`rfp_match.parse_extraction`): the date must be a real calendar
  date within 2 years either side of the received date, else null. Zone
  resolution is a fixed table, never a library guess and never a fixed UTC
  offset: the `timezone` string is uppercased and stripped of periods and
  spaces, then PT, PST, PDT, PACIFIC -> `America/Los_Angeles`; MT, MST, MDT,
  MOUNTAIN -> `America/Denver`; CT, CST, CDT, CENTRAL -> `America/Chicago`;
  ET, EST, EDT, EASTERN -> `America/New_York`; AKST, AKDT ->
  `America/Anchorage`; HST -> `Pacific/Honolulu`; UTC, GMT, Z -> UTC; a
  string `ZoneInfo` accepts verbatim -> that zone; an explicit numeric offset
  (`-07:00`, `UTC-7`) -> that fixed offset; null or anything else ->
  `America/Los_Angeles`. The wall time is localized in the resolved zone on
  the extracted date, so the offset in force that day applies: "2:00 PM PST"
  on a July date is 21:00Z, the same instant the New Bid form stores for
  2:00 PM Pacific. `dateutil` is not used for the zone. Stored as
  `extracted_bid_due_at` (timestamptz) with `extracted_bid_due_has_time`.
- Stored on the row: `extracted_project_name`, `extracted_gc_name`,
  `extracted_bid_due_at`, `extracted_bid_due_has_time`,
  `extracted_bid_notes`, `extract_model`, `extract_prompt_version`,
  `extracted_at`. Then status `match` (attempts reset).
- Failure handling follows classify on the per-step counter: health snapshot
  first via `model_unavailable(snapshot, "rfp_extract")`; wait on connection
  errors and timeouts (no attempt spent); backoff on real failures; `failed`
  at the shared cap. Unusable output twice stores all-null facts, stamps
  `extracted_at` (so the startup backfill never re-runs it), leaves
  `last_error` set (so an unusable model reply stays distinguishable from a
  genuinely nameless email) and advances to `match`, where the no-name path
  lands it at `done` with `flag_reason = no_project_name`.

### 3.2 Match: GC resolution

Pure function `rfp_match.resolve_gc(email, bundle, settings)` over the
per-sweep reference bundle (3.3):

1. Organic sender (`authorization_kind = gc_domain`): the sender address is
   looked up in `gc_contacts.email` (case-insensitive). An exact contact
   gives its `gc_id` and the contact id (kind `contact`). Otherwise the
   sender's domain is matched against the distinct contact domains; one GC
   owning that domain resolves (kind `domain`); several GCs sharing it fall
   through to the name path.
2. Name path: `extracted_gc_name` is normalized (`normalize_company_name`,
   then corporate suffixes and generic words dropped: inc, llc, ltd, corp,
   co, company, construction, constructors, contractors, builders, group,
   the) and scored against every `general_contractors.name` with the same
   trigram Dice plus token containment used for project names. The best
   resolves (kind `name`) when its score is at least
   `RFP_MATCH_GC_AUTO_THRESHOLD` (0.85) and the runner-up is more than
   `RFP_MATCH_RUNNER_UP_GAP` (0.1) lower. Otherwise `resolved_gc_id` stays
   null and the top three are stored in `gc_candidates` for the review
   screen.
3. A human choice on the review screen stores kind `human`. A sibling copy
   stores kind `sibling`.

Stored: `resolved_gc_id`, `resolved_gc_contact_id` (organic exact contact
only), `gc_match_kind`, `gc_match_score`, `gc_candidates`.

### 3.3 Match: reference data, candidates and scoring

Reference bundle, once per sweep. Built lazily on the first row that reaches
`match` (most ticks have none): (a) `general_contractors (id, name)`; (b)
`gc_contacts (id, gc_id, email)` paged once; (c) ONE projects query over the
rebid lookback with `project_gcs (id, gc_id, needs_by,
general_contractors(name))` embedded, built by
`rfp_match.candidate_query(sb, lo_iso, select=...)` (PostgREST, no RPC; the
same shape as `services/due_reminders.py`; `lo_iso` a Z-suffixed UTC literal
computed in Python by `rfp_match.pg_ts`, because PostgREST filters accept
`now()` but not `now() - interval` or `coalesce()`):

```
sb.table("projects").select(<columns>, project_gcs(id, gc_id, needs_by, general_contractors(name)))
  .is_("abandoned_at", "null")
  .not_.in_("current_stage", ["declined", "pm_only", "cp_only"])
  .or_(f"actual_bid_at.gte.{lo},and(actual_bid_at.is.null,internal_bid_at.gte.{lo})")
```

That or-group is `coalesce(actual_bid_at, internal_bid_at) >= lo`. The
30-day candidate window, the rebid band (older than the window, inside the
lookback), and the per-email `excluded_project_ids` are Python filters over
that one result, which also supplies the `needs_by` dates for scoring and
the GC names for the 3.4 prompt. `resolve_gc`, the scorer and the rebid
lookup take the bundle as parameters; nothing in `_step_match` queries these
tables per row. After a merge inside the sweep the step appends the new
`project_gcs` row to the in-memory bundle so a second email from the same GC
in the same tick routes to `duplicate` directly. `_run_from` never runs
`match`, so the bundle is sweep-only.

Required name. When `extracted_project_name` is null or normalizes to an
empty string, the match step runs GC resolution only (so the GC is recorded
for the drawer and the later creation slice), skips the candidate scoring,
the rebid lookup and the LLM verdict, stores `match_candidates = []`,
`match_project_id` and `match_score` null, the `match_weights` snapshot and
`matched_at`, and lands at `done` via `_terminal` with `flag_reason =
no_project_name`, `decided_at_step = match`.

Name score (`rfp_match.name_score`, signature in section 11), all pure:

- Reference numbers are removed before tokenizing, on both sides: (a) a
  leading reference group at the start of a name (digits with optional dots
  or dashes and an optional trailing letter, e.g. `25.7.6826`, `6370`,
  `26.6.7096B`, followed by a dash, colon or space); (b) anywhere in the
  project name, the project's own `projects.number` and its dotted, dashed
  and squashed variants; (c) a reference keyword (bid, itb, ifb, rfp, rfq,
  solicitation, no, number, #) together with the numeric or alphanumeric
  token group that immediately follows it, whatever its length. Exception:
  the counter words (no, number, #) are not reference keywords when a bare
  integer of one to three digits follows ("Fire Station No. 7", "Pump
  Station No. 12"): the number is kept as a discriminator on both sides,
  so "No. 7" and "No. 12" conflict the way "Fire Station 7" and "Fire
  Station 12" do, and the counter word itself is dropped ("Fire Station
  No. 7" and "Fire Station 7" normalize alike). A bare integer of four or
  more digits, or a dotted, dashed or lettered group, behind a counter word
  is still a reference ("Bid No. 1234", "Solicitation No. 2024-15").
- Normalize: NFKC, lowercase, punctuation to spaces, drop stop tokens
  (project, bid, bids, rfp, rfq, itb, ifb, invitation, invite, to, the, of,
  for, and, at, a, an, package, proposal, request, quote, electrical, new,
  re, fw, fwd, reminder), collapse whitespace. If either side normalizes to
  empty, the score is 0.0 and is recorded as present (never absent, never
  1.0 from two empty padded strings).
- Trigram Dice coefficient over the padded normalized strings.
- Token containment: `|A ∩ B| / min(|A|, |B|)`, counted only when the smaller
  side has at least two tokens, or one token of six or more characters. This
  is what lets "Sunrise Elementary" match "Sunrise Elementary School
  Modernization". Containment deliberately scores a shortened name 1.0
  against every longer name that contains it; the runner-up gap in 3.5 is
  what keeps that from auto-merging into one of several siblings (Fire
  Station 7 vs Fire Station 12 when the email names neither).
- `score = max(dice, containment)`.
- Discriminator tokens: pure numbers, single letters, and the token after
  phase, bldg, building, pkg, package, unit, lot, area, zone, wing. Before
  comparing, standalone roman numerals i to xii become digits, ordinals
  (1st, 2nd, 3rd, 4th ...) become digits, and 4-digit year-like numbers
  (19xx or 20xx, the `_YEARLIKE` rule from `email_match.py`) are never
  number discriminators.
- Conflict, per kind: the smaller value set is not a subset of the larger. A
  value present on one side only is never a conflict (a shortened or
  extended name). For the keyword kind the comparison is per keyword (phase
  against phase, building against building). On a conflict the score is
  capped at `RFP_MATCH_CONFLICT_CAP` (0.4) and the breakdown's `conflict`
  field names the kind and the two values.

Date score (`rfp_match.date_score(email_due, has_time, candidate_dates,
settings)`):

| relation to the closest candidate date | score |
|---|---|
| email has no due date | absent (weight dropped) |
| exact timestamp and the email gave a time | 1.0, and the exact-time bonus applies |
| within `RFP_MATCH_BID_DATE_TOLERANCE_DAYS` (3), same calendar day included | 1.0 |
| within `RFP_MATCH_BID_DATE_FAR_DAYS` (14) | `RFP_MATCH_DATE_SCORE_FAR` (0.5) |
| farther | 0.0 |

Exact means: the email's `extracted_bid_due_at` and the candidate's
timestamp date (`actual_bid_at`, else `internal_bid_at`) are the same instant
after both are truncated to the minute, and `extracted_bid_due_has_time` is true.
`needs_by` is a date and can never earn the bonus; it is compared by
calendar date in Pacific. Candidate dates are `actual_bid_at` (or
`internal_bid_at` only when `actual_bid_at` is null) plus every
`project_gcs.needs_by`; `dates_used` therefore never contains both `actual`
and `internal`. Distance is the absolute difference in calendar days between
the two instants converted to `America/Los_Angeles` (`needs_by` is already a
date); 0 to 3 days is inside the tolerance, 4 to 14 days is far.

Invariant: inside the tolerance the date signal can only raise the total,
never lower it: for any name score n and any notes state,
total(n, date inside tolerance) >= total(n, no date). Only a closest date
outside the tolerance scores the candidate down, and blocks the confident
rule regardless of the weights (3.5 rule 2), so such a candidate is
review-only; this is deliberate.

Notes score: trigram Dice between the extracted notes and the project's
`bid_notes`, computed only when both are non-empty. Notes never enter the
weighted average; they are a bonus: `notes_bonus = RFP_MATCH_WEIGHT_BID_NOTES
* dice` when `dice >= RFP_MATCH_NOTES_MIN` (0.5), else 0. Invariant:
total(with notes) >= total(without notes) for every input.

Total: `sum(w_i * s_i) / sum(w_i)` over the present signals among name and
date, plus the exact-time bonus, plus the notes bonus, capped at 1.0.
Defaults: name 0.6, date 0.3, notes bonus up to 0.1. Derivation of the
floors: with a date inside the tolerance the total reaches 0.85 at
n >= 0.775, so `RFP_MATCH_NAME_MIN_AUTO` (0.8) is the binding floor when a
date is present; with no date the total equals n, so
`RFP_MATCH_NAME_MIN_AUTO_NO_DATE` (0.9) is the binding floor.

Breakdown per candidate: `{name, date (or null), notes (raw Dice or null),
notes_bonus, exact_time, total, conflict, dates_used, closest_kind}`.
`dates_used` is a list of kinds only (`actual`, `internal`, `needs_by`) and
`closest_kind` the kind the date score was taken from. No date VALUE from
the candidate project is ever written into a breakdown.

Stored on every match decision, whether or not the model ran:
`match_candidates` (the top `RFP_MATCH_MAX_CANDIDATES` (5) by total, always,
so near misses are visible on the review screen and for training; each
entry `{project_id, name, number, breakdown, verdict, confidence, reasoning}`
with verdict, confidence and reasoning null for a candidate the model did
not see), `match_weights` (the full resolved `rfp_match_*` settings except
model names, plus `scorer_version` and `auto_merge_enabled`), `matched_at`.
`match_project_id` and `match_score` are the best ranked candidate after the
confident-different filter when its total is at or above the review
threshold (every `review_match` reason, `merged`, `duplicate`); null for
`no_project_name`, `no_candidate` and `all_different`. `match_llm_model` and
`match_llm_prompt_version` are set only when the model was called. `match_candidates` and `match_weights` always
reflect the latest evaluation and are replaced whenever the row passes
`match` again; `rfp_project_matches` is the durable record of every
decision and is never replaced by a later evaluation (the only in-place
change is 3.6 step 3's kind flip). The list route never selects
`match_candidates`; only the detail route returns it.

Wider rebid lookup (`rfp_match.rebid_lookup(name, projects, settings)`, where
`projects` is the rebid band the caller slices from the bundle: older than
the candidate window, inside the lookback, minus excluded ids): runs
only when routing is about to land the row at `review_match` or `done`
(never for `merged` or `duplicate`). Name-scores the projects in the rebid
band and stores the best at or above `RFP_MATCH_REBID_NAME_THRESHOLD` (0.85)
as `possible_rebid_project_id` with `possible_rebid_score`. A re-run at
`match` nulls both first. Never merges. The same function serves the New
Bid check.

### 3.4 Match: LLM verdict

- Feature `rfp_match`, same routing and fallbacks as extract. Health label
  "RFP project matching".
- Called only when at least one candidate's total is at or above
  `RFP_MATCH_REVIEW_THRESHOLD` (0.55). The members of the stored top-5 with
  total at or above the threshold go in, so every judged candidate is in the
  stored list by construction.
- Prompt (`rfp_match.build_match_messages`): the extracted facts (project
  name, GC name, due date and time, notes cut to 500 characters) go inside
  the same `<<<EMAIL_START>>>` / `<<<EMAIL_END>>>` block classify uses, with
  both marker strings scrubbed from all four values first; the candidate
  list (index, name, number, bid dates in Pacific, bid notes, GC names) goes
  outside the block. The system prompt states that the block is untrusted
  text copied from an outside email, that instructions inside it are to be
  ignored, and that the candidate list is our own record. It asks, per
  candidate, whether the email is about the same construction project, with
  the explicit note that a shortened, reordered, extended or misspelled name
  of the same job is "same", and that a different phase, building, package
  or site is "different".
- Output schema (enforced):

  ```json
  {"verdicts": [{"index": 0, "verdict": "same | different | unsure", "confidence": 0.0, "reasoning": "..."}]}
  ```

  `rfp_match.parse_verdicts` normalizes every entry before storage: a
  verdict outside the enum becomes `unsure`, confidence goes through
  `clamp_confidence`, reasoning through `truncate_words` (20 words), entries
  whose index is not in the sent list are dropped, and a sent candidate with
  no entry is `unsure`.
- The verdict is an AND gate, never a lift: a `same` verdict cannot raise a
  candidate whose deterministic total, name floor or conflict cap fails the
  confident rule; a `different` verdict only parks the row.
- Failure handling as extract, on the per-step counter
  (`model_unavailable(snapshot, "rfp_match")`), so "twice" means twice at
  `match`. Unusable output twice routes the row to `review_match` with the
  deterministic ranking, no verdicts, `flag_reason = match_llm_unusable`.
- Everything after a successful model call, at extract and at match (the
  CAS, the merge or duplicate the system runs in 3.6), runs under the
  ordinary retry ladder (`_retry_or_fail`): an exception that is not an
  `RfpMatchError` (a `ProposalSendError`, a unique violation, a PostgREST
  error) spends an attempt, sets `next_attempt_at` with backoff and fails
  at the cap with `flag_reason = match_error` (or `extract_error`). It never
  propagates to the sweep, which would leave the row at its step with
  `attempts = 0` and the model called again every tick. An `RfpMatchError`
  (the project closed, the GC left it, the row moved) parks the row at
  `review_match` with `match_uncertain` as before.

### 3.5 Match: routing

With `T = RFP_MATCH_LLM_CONFIDENCE_THRESHOLD` (0.8). Rank the candidates
whose verdict is not a confident `different` by total; best is the first,
runner-up the second.

0. No usable name (3.3): `done`, `flag_reason = no_project_name`. Rules 1 to
   7 are not evaluated.
1. Ambiguity guard: if `best.total >= RFP_MATCH_REVIEW_THRESHOLD` and a
   runner-up exists with `runner_up.total >= best.total -
   RFP_MATCH_RUNNER_UP_GAP`, the row goes to `review_match` (`flag_reason =
   match_ambiguous`) regardless of the rules below; never `duplicate` or
   `merged` automatically.
2. A candidate is "confident" when: total >= `RFP_MATCH_AUTO_THRESHOLD`
   (0.85); name score >= `RFP_MATCH_NAME_MIN_AUTO` (0.8), or
   `RFP_MATCH_NAME_MIN_AUTO_NO_DATE` (0.9) when the email has no due date;
   no conflict cap; when the email has a due date, its date score is 1.0
   (the closest candidate date is inside the tolerance); verdict `same`
   with confidence >= T.
3. The sender is "verified" when `auth_dmarc = pass` or `auth_compauth =
   pass` (both stored by the 0120 fetch step; this is an extra gate on the
   automatic decision only, `auth_verdict` is unchanged) AND
   `authorization_kind` is `address`, `domain` or `gc_domain` (never
   `override`).
4. Best confident, sender verified, GC resolved, GC already on that project,
   auto-merge on: `duplicate`.
5. Best confident, sender verified, GC resolved, GC not on the project,
   auto-merge on: `merged`.
6. Best confident but any of: GC unresolved, sender unverified, auto-merge
   off: `review_match`, with `flag_reason` by fixed precedence
   `match_gc_unresolved`, then `match_sender_unverified`, then
   `match_confident`. `match_confident` is written only when the auto-merge
   switch is the sole reason the row did not merge, so the match-stats tally
   measures exactly what turning the switch on would have done.
7. Not confident, but some candidate has total >= the review threshold and
   is not a confident `different`: `review_match` (`match_uncertain`).
8. Otherwise: `done` with `flag_reason = no_candidate` (nothing scored at or
   above the review threshold, or the window was empty; rule 1 never applies
   when the best is below the review threshold) or `all_different` (the
   model called every judged candidate a confident `different`).

Every route stamps `decided_at_step = match` and `matched_at`. The system
routes to `merged` and `duplicate` write `flag_reason = null`; the status
says it. Human actions never touch `flag_reason` (unlike `review()` at
classify), so the agreement tally below stays a count query.

Crash resume before any of this: when the match step finds an open
`rfp_project_matches` row of `kind = merged` for the email (a system merge
that crashed between 3.6 steps 5 and 6, the email still at `match`), it
calls `merge_email` on that row's project and GC directly, without GC
resolution, scoring or a model call. Re-scoring could land the email
somewhere else while the first target keeps the link. An open `duplicate`
row does not short-circuit (3.6 step 2 closes it).

`review_match` rows count toward the sidebar badge, the dashboard task card
and the per-tick review notification. `_TickStats` gains `match_review_new`
and `merged_new`; `_notify_review_queue` adds "N matches to review" to the
existing `rfp_email.review` summary (same dedup).

### 3.6 Merge (system or human)

`rfp_email_ingest.merge_email(sb, email_id, project_id, gc_id, actor_id |
None, *, bundle=None)`. The human path first re-reads the email and refuses
with 409 `rfp_match_not_actionable` unless `status = review_match`; this
narrows the race window but cannot close it (steps 2 to 5 are inserts into
other tables), so step 6 compensates on a miss. Order, so a crash at any
point resumes cleanly:

1. Re-read the project; refuse (LookupError, 409 `rfp_match_project_closed`)
   if it is abandoned, declined, PM-only or CP-only, or outside the
   candidate window (`coalesce(actual_bid_at, internal_bid_at)` older than
   `RFP_MATCH_CANDIDATE_WINDOW_DAYS`, or null), so the backend and the
   drawer's search agree. Refuse with 409
   `rfp_match_project_excluded` if `project_id` is in the email's
   `excluded_project_ids` (this covers merge and duplicate, system and
   human). For a human merge whose project is not in `match_candidates`
   (allowed: the reviewer may search any open project in the candidate
   window), score that project live with the deterministic scorer (no LLM)
   so the stored breakdown describes the target; `candidate_rank` is null.
2. If an open `rfp_project_matches` row already exists for this email
   (`unmerged_at is null`; the partial unique index guarantees at most one):
   with `kind = merged` and the SAME project and GC, a merge request is a
   crash-resume, so `gc_added` true goes to step 6 and otherwise step 3's
   existence check decides; with `kind = merged` and a DIFFERENT project or
   GC the action is refused (409 `rfp_match_not_actionable`: two reviewers
   merged at once, or a system merge is mid-flight; the second reviewer
   reloads, and Unmerge on the first project is the way to move it). The
   open merged row's link is never removed here, whoever asks. A `kind =
   merged` row under a duplicate request (same target), or any open `kind =
   duplicate` row (a reopen that crashed between its two writes), is a
   leftover: closed first (`unmerged_at`, `unmerged_by` = the actor or
   null, `unmerge_reason = superseded`) and the action inserts a fresh row
   of the requested kind. A merged row is never resumed as a duplicate and
   the duplicate path never resumes: it always inserts (the link the
   crashed merge put on the project stays, and from then on reads as an
   ordinary GC on the project). Insert the row: `kind = merged`, `gc_added = false`,
   `score` from the chosen candidate's total and `breakdown` as its breakdown
   object merged with its verdict, confidence and reasoning, `candidates`
   a copy of the email's `match_candidates`, `weights` the snapshot,
   `candidate_rank` (1-based position by total, null when not listed),
   `decided_by` (null for the system), and the provenance columns copied
   from the email (`gc_match_kind`, `gc_match_score`, `authorization_kind`,
   `invitation_method`, `sender_address` from `rfp_emails.from_address`
   lowercased, `auth_dmarc`, `auth_compauth`).
3. Insert `project_gcs {project_id, gc_id, needs_by, rfp_match_id: <row id>}`
   where `needs_by` is the extracted due date as a Pacific calendar date, or
   null. On a unique violation, or on any resume with `gc_added = false`,
   select the existing link by (project_id, gc_id): if its `rfp_match_id`
   equals this row's id, step 3 already completed and the merge continues;
   otherwise the GC arrived independently (a person or project creation),
   the match row becomes `kind = duplicate` (keeping `score`, `breakdown` and
   `candidate_rank`), step 6 writes the outcome with `match_review_decision =
   duplicate` on the human path, and step 7 audits `rfp_match.duplicate` with
   `{requested: merge}`; the email lands at `duplicate`. The unique violation
   alone never decides.
4. Organic sender with a known contact at that GC: insert the
   `project_gc_contacts` selection for that one contact through
   `_insert_ignore` on (project_gc_id, gc_contact_id) and record
   `contact_selected_id`.
5. Update the match row: `gc_added = true`, `project_gc_id`, in one write.
6. CAS the email from `match` (or `review_match`) to `merged`, setting
   `match_project_id` and `match_score` to the chosen candidate so the two
   rows agree, and for a human merge `match_review_decision = merge`,
   `match_review_by/at`, `match_review_agreed` (true when the chosen project
   equals the system's best). On a CAS miss (another reviewer or the sweep
   moved the email first): compensate before returning. If `gc_added`,
   remove the link through `remove_gc_link(link_id=..., via_unmerge=True)`
   (tolerant of an already-missing link; it also drops the step-4 selection
   and retires anything unsent), then delete the match row (nothing happened
   from the project's point of view), audit `rfp_match.lost_race`, and raise
   LookupError so the router returns 409 `rfp_match_not_actionable`. If the
   compensation itself fails, log and audit it and leave the row open so it
   stays visible and hand-unmergeable on the project.
7. Audit `rfp_match.merge` with `project_id`, `gc_id`, `candidate_rank`,
   `decided_by` and the provenance fields. No per-merge notification: a
   system merge increments the tick's `merged_new`; a human merge notifies
   nobody (the actor just did it; the audit row and the project modal are
   the record).

Duplicate: the same, minus steps 3 to 5, `kind = duplicate`, status
`duplicate`, `match_review_decision = duplicate` on the human path, audit
`rfp_match.duplicate`. The human "already on X" action requires the resolved
GC to be on X (409 `rfp_match_gc_not_on_project`).

Per-tick merge notification: one `rfp_match.merged` bell row to the
Estimating Admin per tick that merged anything ("The matcher added a GC to
N project(s)", `metadata = {merged: N, project_ids: [...]}`, `project_id`
null), deduped against an unread, undismissed row of the same type,
`mirror_email = False`. `NotificationsBell` routes it to
`/rfp-emails?tab=processed`, like `rfp_email.review`. Duplicates and human
merges emit nothing.

### 3.7 Unmerge

Ordinary GC removal first. `DELETE /projects/{id}/gcs/{gc_id}` and the shared
`proposal_send.remove_gc_link(project_id, gc_id, *, link_id=None,
refuse_if_sent=False, via_unmerge=False) -> bool` look for an open
`rfp_project_matches` row for that project and GC (`kind = merged`,
`unmerged_at is null`, regardless of `gc_added`, so a crash-resume row is
covered). If one exists and `via_unmerge` is false, refuse with
`ProposalSendError(409)` mapped to `rfp_match_unmerge_required`; the message
points at Unmerge in the Merged by System modal. Only `unmerge()` passes
`via_unmerge=True`. The helper otherwise runs the sequence the DELETE route
runs today, extracted into `proposal_send.py` next to
`retire_unsent_proposals`:

a. `retire_unsent_proposals(project_id, gc_id)` FIRST. This is the claim: a
   conditional update on `status in (generated, failed)`, the same rows and
   condition `send_proposals` (claim to `sending`) and `mark_submitted`
   (claim straight to `sent`) update, so the row lock makes exactly one side
   win.
b. Delete the link through the SQL function
   `remove_project_gc_unless_sent(p_link_id uuid, p_project_id uuid, p_gc_id
   uuid, p_refuse_if_sent boolean) returns uuid` (migration 0122): deletes
   `project_gcs where id = p_link_id and project_id = p_project_id` unless
   (`p_refuse_if_sent` and a `proposal_sends` row for the pair is `sent` or
   `sending`) or (not `p_refuse_if_sent` and a row is `sending`), returning
   the deleted id or null. With `link_id` null the function resolves the pair
   to its current link. A null return with a live send row means a send got
   in between: raise `ProposalSendError(409)` (unmerge: mapped to
   `rfp_match_gc_already_sent`; DELETE route: the existing "send in
   progress" 409), nothing is detached, the match row is untouched. A null
   return with no link at all means already removed: return False, never
   raise (the 404 stays in the DELETE route). On a null return the helper
   selects `project_gcs` by `link_id` when one was passed, otherwise by
   (project_id, gc_id): a row present means the function refused because of
   a send, raise 409; no row means already removed (or re-added under a new
   id when `link_id` was passed), return False. `proposal_sends` is never
   queried to interpret the null.
c. `dismiss_notifications(project_id=project_id,
   types=["gc_pricing.approval_requested"], gc_id=gc_id)` (the helper
   requires a project or RFQ scope; `gc_id` only narrows it).

The DELETE route becomes a thin wrapper: its 404 pre-check, the helper with
`refuse_if_sent=False` (today's behavior: a GC with a sent proposal may
still be removed by hand), `ProposalSendError` to its status code,
`audit("project.gc_remove")`, `project_gc_rows`. Its `proposal_sends` and
notification end state for a GC with a generated proposal and a pending
pricing approval is unchanged.

`rfp_email_ingest.unmerge(sb, match_id, reason, actor_id)`:

1. Roles: Estimating Admin, Executive, IT Admin (router). Reason required,
   3 to 500 characters.
2. Load the match row by id; it must be `kind = merged`. If it is already
   closed (`unmerged_at` set) and its email is still at `merged` with
   `match_project_id = project_id`, run only steps 6, 6b and 7 (the finish
   path) and return 200. If it is closed and the email has moved on, 409
   `rfp_match_not_actionable`.
3. Pre-check, cheap 409 before anything is touched: a `proposal_sends` row
   for that project and GC at `sent` or `sending` refuses with 409
   `rfp_match_gc_already_sent`; the GC stays. The authoritative guard is
   step 4b.
4. If `gc_added` and `project_gc_id` is not null, remove that specific link
   through `remove_gc_link(project_id, gc_id, link_id=project_gc_id,
   refuse_if_sent=True, via_unmerge=True)`. A uuid pk plus the `on delete
   set null` FK is the proof the row is the one the merge inserted; a GC a
   person removed and re-added after the merge has a different id and is
   never deleted. If `gc_added` is true but `project_gc_id` is null, a person
   already removed the system's link; nothing is removed. If `gc_added` is
   false, nothing is removed. In every case the remaining steps run.
5. Stamp `unmerged_at`, `unmerged_by`, `unmerge_reason` with a conditional
   update `where id = match_id and unmerged_at is null`. Zero rows means
   another unmerge finished first: 409 `rfp_match_not_actionable`.
6. Email: append the project to `excluded_project_ids`, clear
   `match_project_id` and `match_score`, null `possible_rebid_*`, clear
   `match_review_decision`, `match_review_by`, `match_review_at` and
   `match_review_agreed` (the reversed decision lives on the closed match
   row), reset `attempts`, `last_error`, `next_attempt_at`, leave `match_candidates` and
   `match_weights` as they are (the next tick replaces them; the decision's
   own copies stay on the closed match row), CAS `merged -> match` as a
   conditional update on `status = merged and match_project_id =
   project_id`. Best-effort: a miss (the email is no longer at `merged`)
   does not fail the unmerge. The sweep re-runs the match on the next tick
   (the LLM call must not ride a request). If nothing else matches, the row
   lands at `done`.
6b. Cascade, whenever the row has `gc_added = true` and, after step 4,
   `project_gc_id` is null (the `on delete set null` FK is the durable proof
   the system's link is gone), on the first pass and on every retry including
   the finish path: select every
   `rfp_project_matches` row with `kind = duplicate`, the same `project_id`
   and `gc_id`, `unmerged_at is null` and `decided_at >= this row's
   decided_at`. For each: stamp `unmerged_at`, `unmerged_by` = the same
   actor, `unmerge_reason = "cascade: " + reason`; on its email apply step
   6 (the same field list) with a CAS on `status = duplicate and
   match_project_id = project_id` (a row no longer at `duplicate` is
   skipped). Audit one `rfp_match.unmerge_cascade` per row with the parent
   match id. Duplicates decided before the merge, for another GC, or on a
   merged row whose GC was already on the project are untouched, because
   their premise still holds. Unmerge asserts the email is not about that
   project; the cascade extends the assertion to the duplicates that only
   existed because this merge put the GC on the project.
7. Audit `rfp_match.unmerge` with `entity = project`, `entity_id =
   project_id`, payload `{match_id, rfp_email_id, gc_id, reason, gc_removed:
   true|false, project_gc_id}` where `gc_removed` is the same stored test as
   6b, so it appears under the project's audit filter and never claims the
   system's link was removed when it was not.

A retry after a crash at any point re-enters at step 2 and finishes: an open
row whose link is already gone passes through step 4 as a no-op; a closed
row whose email is still at `merged` runs only steps 6, 6b and 7.

Several GCs merge into one project over time; each is its own row and its
own unmerge. Duplicate rows are not unmerged (no link was added); a wrong
duplicate is reversed with `reopen` (3.8), which closes the match row and
returns the email to `review_match`.

### 3.7b Acknowledge (no proposal needed)

`rfp_email_ingest.acknowledge_match(sb, match_id, reason | None, actor_id)`.
Writer roles (the same set that adds or removes a GC today). Reason
optional, at most 500 characters. The row must be `kind = merged`, open and
not already acknowledged; refuse with 409 `rfp_match_not_actionable` when
the GC already has a `sent` or `sending` proposal (nothing to acknowledge).
Stamps `acknowledged_by`, `acknowledged_at`, `acknowledge_reason`; leaves the
GC on the project and the email at `merged`; audits `rfp_match.acknowledge`.
It does not block a later unmerge (until sent) or a later Send proposal from
the sidebar: it only says no proposal is needed now, which clears the New RFP
pill.

### 3.8 Human actions on `review_match`

Merge, already on X, not a match and set GC pre-read `status =
review_match` and refuse otherwise (409 `rfp_match_not_actionable`); reopen
pre-reads `done` or `duplicate`. Reject and set GC are single conditional
updates; merge and already-on-X write the match row (and the link) before
their CAS and compensate on a miss as 3.6 describes. Exactly one of two
concurrent actions on the same email takes effect. Merge, already on X and
not a match stamp `decided_at_step = review_match`; set GC and reopen leave
it as it is. Every action is audited once.

| action | effect |
|---|---|
| merge into X | `merge_email` with `decided_by` set. Requires a resolved GC (`resolved_gc_id`, or `gc_id` in the body, which also stores kind `human`); 400 `rfp_match_gc_required` when neither is set. A malformed `project_id` or `gc_id`, or an unknown GC, is a 404 in the router before the service runs (the service re-checks the body GC and raises a bare LookupError before any write). X must not be excluded (409 `rfp_match_project_excluded`); X may be any open project in the candidate window (scored live when not in `match_candidates`). Silent for the actor. |
| already on X | duplicate path with `decided_by`. 400 `rfp_match_gc_required` when `resolved_gc_id` is null (set GC first), else 409 `rfp_match_gc_not_on_project` when the GC is not on X. |
| unmerge (by email) | resolves the email's latest `kind = merged` row (open or closed, via the `(rfp_email_id, decided_at desc)` index) and calls `unmerge(match_id, reason, actor_id)`; 404 when the email has no merged row. Estimating Admin, Executive, IT Admin. |
| not a match | No GC needed. Writes `match_review_decision = no_match`, `match_review_agreed = false`, status `done`; `flag_reason` untouched. The candidates stay on the row for training. Refused (409 `rfp_match_not_actionable`) while an open `kind = merged` row exists for the email (a merge that crashed before its email write still holds a link on the project): merge again to finish it, or unmerge it on the project. |
| set GC | `resolved_gc_id = gc_id`, `gc_match_kind = human`. Stays in `review_match`. The GC may have just been created through the existing `POST /gcs`. A malformed or unknown `gc_id` is a 404 in the router. |
| reopen | on `done` or `duplicate` (not `review_match`). Conditional update to `review_match`; clears `match_review_decision`, `match_review_by`, `match_review_at`, `match_review_agreed`, `last_error`, `next_attempt_at`; `flag_reason` untouched (the reopen is recorded by its audit row). On a `duplicate` row, first CAS `duplicate -> review_match`, then close its open match row (`unmerged_at`, `unmerged_by`, `unmerge_reason = reopened` plus the optional reason); a crash between the two writes is harmless because 3.6 step 2 closes a leftover open duplicate row before any later merge or duplicate inserts. Nothing on the project is touched. Audit `rfp_match.reopen`. Review-queue roles. |

`match_review_agreed` (0122): true when the human's project equals
`match_project_id` (merge or duplicate into the system's best), false
otherwise (a different project, or no match). Null until reviewed.
`GET /rfp-emails/match-stats` (review-queue roles, default bucket, read when
the Matches tab mounts and after each action) returns
`{confident: {pending, agreed, disagreed}}` from three count-only reads over
`rfp_emails where flag_reason = match_confident` split by `status =
review_match`, `match_review_agreed = true`, `match_review_agreed = false`.

### 3.9 Projects side

- `projects.bid_notes` (text) and `projects.is_rebid` (boolean, default
  false) are added to `ProjectCreate`, `ProjectUpdate` and `ProjectOut` in
  `app/models/schemas.py`, and both keys to `_FIELD_EDITORS` in
  `routers/projects.py` mapped to `_OPEN` (writer roles); `update_project`
  403s any key missing from that map.
- An "open merge" is a row with `kind = merged`, `unmerged_at is null` and
  `project_gc_id is not null`. `ProjectOut` gains `rfp_merged_count` (open
  merges; duplicates never count), `rfp_history_count` (rows with `kind =
  merged` and `unmerged_at` set, open merged rows whose `project_gc_id` is
  null, plus every `kind = duplicate` row, so any row makes the modal
  reachable) and `rfp_new_count` (the subset of open merges on a project
  where `proposal_send.bid_has_gone_out` is true,
  `acknowledged_at` is null, and the GC has no `proposal_sends` row at
  `sent` or `sending`). All three default to 0. One helper,
  `_rfp_match_counts(project_ids, projects_by_id, cat_states) -> dict`, next
  to `_pending_gc_pricing_counts`, serves the list and detail routes: it
  returns `{}` immediately when `not get_settings().rfp_ingest_enabled`
  (no query against a table that does not exist on deployments without
  0122); otherwise (1) one query over `rfp_project_matches` selecting
  `project_id, gc_id, kind, unmerged_at, project_gc_id, acknowledged_at` for
  the listed ids, served by the `(project_id, decided_at desc)` index; (2)
  if no open merged rows came back (the normal case), `rfp_new_count` is 0
  everywhere and `proposal_sends` is not queried; (3) otherwise, restrict to
  the projects that are post-send by the same rule as `bid_has_gone_out`,
  evaluated inline over the loaded data (send_out head from the category
  states in `PRICING_APPROVAL_HEADS`, or head `verify` with the project row's
  `reverify_return_stage` in that set; `bid_has_gone_out` itself queries
  `projects` per call and is not used here; a pure `gone_out_rule(head,
  reverify_return_stage) -> bool` that `bid_has_gone_out` delegates to is
  the clean way) and run one `proposal_sends` query for those
  project ids with `status in (sent, sending)`. Never query `proposal_sends`
  over the full listed id set (list_projects is unpaginated). `_present`
  takes the counts as an optional dict; create, update, abandon, reactivate
  and bids-today leave the defaults, as `gc_pricing_approvals_pending` does
  today (every consumer of those responses refetches).
- `proposal_send.project_gc_rows` (the shape behind `_project_gc_rows`,
  `GET /projects/{id}/gcs`, the add, patch and delete responses and the
  pricing-approval responses) gains `rfp_match_id` (the open merged row's
  id, or null) and `rfp_system_added` (`{gc_match_kind, sender_address,
  invitation_method}` for such a row, else null), from one extra
  `rfp_project_matches` read (`project_id`, `kind = merged`, `unmerged_at is
  null`) keyed by `gc_id`.
- `GET /projects/{id}/rfp-matches`: every match row for the project, `decided_at
  desc`. Match metadata for every internal role: match id, kind, GC id and
  name, `gc_added`, `gc_on_project` (the link still exists), the GC's current
  proposal status, `score`, `scorer_version` from the snapshot,
  `decided_by` name (null rendered as "System"), `decided_at`,
  `acknowledged_at`, `acknowledge_reason`, acknowledging user's name,
  `unmerged_by` name, `unmerged_at`, `unmerge_reason`, `can_unmerge` with its
  reason (`sent`, `role`, `closed`), and the provenance fields
  (`gc_match_kind`, `sender_address`, `invitation_method`). An `email` block
  (subject, sender, received time, extracted facts, breakdown, candidates,
  weights snapshot) only when the caller's role is in `RFP_REVIEW_ROLES`;
  null otherwise (the accountant never sees email content). Breakdown and
  candidates pass through `rfp_match.redact_candidates(row, role)` for roles
  outside `ACTUAL_BID_VIEWER_ROLES`.
- `POST /projects/{id}/rfp-matches/{match_id}/unmerge {reason}`: Estimating
  Admin, Executive, IT Admin.
- `POST /projects/{id}/rfp-matches/{match_id}/acknowledge {reason?}`: writer
  roles.
- Gating: the three routes above carry `dependencies=[Depends(require_rfp_ingest)]
  + _BIDDING_ONLY` (the master `RFP_INGESTION_ENABLED` switch, not the
  derived mailbox setting, so records stay visible and unmergeable while
  polling is paused); they 404 with the bare body when either flag is off.
- `POST /projects/similar {name}`: writer roles, `_BIDDING_ONLY` only (New Bid
  code, never gated on the RFP flag), default rate bucket. Any date in the
  body is ignored (name-only schema). Window: `internal_bid_at >= now() -
  RFP_MATCH_PRECREATE_WINDOW_DAYS` (60), never coalesced with the actual
  date, so membership is a function of a date every caller can see (the
  bids-today rule); same stage and abandoned exclusions. Scores with
  `name_score` alone; the per-candidate breakdown is `{name, total,
  conflict}` with total equal to the name score. Response `{similar: [...],
  possible_rebids: [...]}`: `similar` holds candidates with total >=
  `RFP_MATCH_PRECREATE_THRESHOLD` (0.5); `possible_rebids` is `rebid_lookup`
  over projects older than the precreate window and inside
  `RFP_MATCH_REBID_LOOKBACK_DAYS`. Both bands of this route use one query on
  `internal_bid_at >= now() - RFP_MATCH_REBID_LOOKBACK_DAYS` with the same
  `abandoned_at` and `current_stage` exclusions, built by a separate
  `rfp_match.precreate_query(sb, lo_iso)` (never `candidate_query`, whose
  or-group reads the actual date); `similar` is the slice with
  `internal_bid_at` inside the precreate window and `possible_rebids` the
  older slice. Item shape: id, name, number, status, stage,
  `internal_bid_at`, `actual_bid_at` (returned only to
  `ACTUAL_BID_VIEWER_ROLES`), GC names, breakdown. No score, membership or
  sort order in this response is a function of the actual date. No snapshot
  is stored; the check is advisory.
- Notification wiring: register `rfp_match.merged` in
  `notification_email._TYPE_META` ("New RFP matches merged", "Open RFP
  emails") so the type has a title if mirroring is ever enabled; today the
  row is bell-only (`mirror_email = False`, `project_id` null) and the bell
  routes it to `/rfp-emails?tab=processed`.

### 3.10 Frontend

- `/rfp-emails`: tab order Review, Matches, Unauthorized, Flagged,
  Processed. Processed holds `done`, `merged` and `duplicate`, with the
  status column telling them apart, `flag_reason` rendered under the
  badge (`no_project_name`, `no_candidate`, `all_different`, `sibling`) and
  `match_review_decision` rendered for human-decided rows. The Matches tab lists
  `review_match` rows; each row shows the extracted project name, the best
  candidate (name, number, link) with `match_score` (null for roles outside
  `ACTUAL_BID_VIEWER_ROLES`, section 8), and the resolved GC or
  "GC unresolved" (the list route attaches `match_project {id, name,
  number}` and `resolved_gc {id, name}` per row the way it attaches
  mailboxes). `GET /rfp-emails/counts` adds `matches`; both consumers of the
  counts include it in their sum: the sidebar badge (`Sidebar.tsx`) and the
  Estimating Admin's dashboard task card (`RfpEmailsTaskCard.tsx`), whose
  description gains a matches segment and whose Open button links to the
  first non-empty tab in tab order.
- Detail drawer, for any row past `extract`: an "Extracted" block (project
  name, GC, due date and time, notes, possible rebid as a link with the
  score). For any row past `match`: the candidate list with breakdowns,
  verdicts and reasoning, project links, and the weights snapshot, so a
  `done` row's near-misses are readable. A Callout at the top keyed on
  `flag_reason`: `match_confident` ("The system is confident this is
  {project}. Auto-merge is off, so confirm it."), `match_gc_unresolved`,
  `match_sender_unverified`, `match_ambiguous`, `match_uncertain`,
  `match_llm_unusable`, and for `done` rows the routing reason. Matches tab
  header: "Auto-merge is off. Reviewers agreed with the system on {agreed}
  of {total} confident matches." from `match-stats`. For `review_match`:
  the GC block (resolved GC with its provenance sentence, or a picker over
  the GC directory with an "Add GC" button that opens the existing
  `CreateGcModal`), a read-only "Unmerged from" line listing
  `excluded_projects` (never offered as targets), the candidate list with
  per-candidate "Merge into this project" or "Already on this project"
  (when the resolved GC is on it), a "Merge into another project" search
  over open projects in the candidate window, and "Not a match". For
  `merged`: the match record with a link to the project and Unmerge (roles)
  with a reason field. For `duplicate` and `done`: "Reopen" with an optional
  reason. A `done` row with a closed match row shows "Unmerged from
  <project> by <name>: <reason>" above the Extracted block. The method block
  is gated by a new `isRfpEmailMethodEditable` predicate (true for every
  status except the six pre-method ones), not by "non-pending", so
  backfilled rows waiting at `extract` can still have their method
  corrected.
- Provenance sentence, shown wherever a system-added GC appears (Merged by
  System modal, Matches drawer, GCs panel chip tooltip): when `gc_match_kind
  = name`, "GC identified from the email text, not from a verified sender
  ({address}, {method})"; when `contact` or `domain`, "GC identified from
  the verified sender {address}". Surfacing the same line on the proposal
  send confirmation modal is a follow-up.
- Project page header: show the badge when `rfp_merged_count +
  rfp_history_count > 0`. Label "Merged by System (N)" with
  `rfp_merged_count` when N > 0; otherwise the same badge in the muted style
  labeled "RFP match history (M)". Both open `RfpMatchesModal` (title
  "Merged by System") with two groups: "Merged" (open rows; per row the
  provenance sentence, `scorer_version`, Unmerge with reason (roles,
  disabled with the reason shown when the proposal is sent or sending), and
  "No proposal needed" (writers; hidden once sent, sending or acknowledged;
  an acknowledged row shows "No proposal needed, {user}, {date}" and the
  reason)) and a dimmed "History" group (unmerged rows with who, when and
  why; duplicate rows labeled "Already on project" with a link to the email,
  no controls). Email details inside the modal render only when
  `canReviewRfpEmails(role)`, mirroring the backend. A "New RFP" pill next to
  the status badge when `rfp_new_count > 0`.
- `ProjectGCsPanel`: a row with `rfp_match_id` shows a "Merged by System"
  chip with the provenance tooltip. For Estimating Admin, Executive and IT
  Admin the Remove button becomes "Unmerge", opening `RfpMatchesModal` at
  that row; for other writers Remove is hidden and the tooltip says the GC
  was added by RFP ingestion and can only be unmerged by those roles. A 409
  `rfp_match_unmerge_required` from a stale page opens the modal.
- Dashboard: a project with `rfp_new_count > 0` stays under Active (the same
  rule the pending pricing approval uses), shows the "New RFP" chip, and
  lists the Estimating Admin among its owners.
- `NotificationsBell`: routes `rfp_match.merged` to
  `/rfp-emails?tab=processed` while the RFP feature is on, otherwise inert,
  like `rfp_email.review`.
- New Bid form (`NewProjectModal`): `bid_notes` via the UI kit `Textarea`
  (hint: "Instructions from the GC about this bid. Not the same as project
  notes.") and an "Is a rebid" checkbox (hint: "Tick when this job was bid
  before under another invitation."). The similar-projects check lives at
  the top of `createAndUpload`, immediately before the `POST /projects`
  branch, not in `onSubmit`: the missing-documents confirm dialog calls
  `createAndUpload` directly, so this is the only place both entries into
  creation pass through. It runs only when `createdId` is null (a retry
  after a partial upload never re-checks) and `similarAccepted` is false.
  It fails open: its own awaited call outside the create try/catch, a 5 s
  client deadline (`signal: AbortSignal.timeout(5000)`), no
  `withRateLimitRetry`; on any thrown error or a malformed body,
  `console.warn` and proceed straight to the create with no modal. Only a
  200 with a non-empty `similar` or `possible_rebids` opens
  `SimilarProjectsModal`, which renders two sections, "Similar projects"
  and "You may have bid this before", each row with name, number, status,
  bid date, GCs, why it matched (the name score), and a link that opens the
  project in a new tab. Buttons: Cancel (closes only this modal; the fully
  populated form, staged files and selected GCs stay, and Save for later
  remains available), Create anyway (sets `similarAccepted`, calls
  `createAndUpload` again), and, only when `possible_rebids` is non-empty,
  Create as a rebid (also sets `is_rebid = true` on the body). The flag
  clears when the name changes. `similarOpen` is lifted to the outer
  `NewProjectModal` and added to `suppressClose` alongside `confirmOpen` and
  `gcCreateOpen`, so Escape closes the stacked modal rather than the form.
  The draft-to-project path is the same form with the same two entry points.
- Project details modal (`EditProjectDetailsModal`): `bid_notes: string |
  null` and `is_rebid: boolean` join `ProjectDetailsFields` and the page's
  `Project` interface. `bid_notes` joins the string-valued state (initial
  `project.bid_notes ?? ""`) so the existing diff loop sends it and clears
  it with null, rendered with the UI kit `Textarea` (widen `set()`'s event
  type). `is_rebid` is a boolean tracked like `is_ngem`: its own state, a
  term in `dirty`, and its own patch line. The project page shows both.
- Vocabulary mirrors that must move together: `lib/rfpEmails.ts`
  (`RfpEmailTab` / `RFP_EMAIL_TABS` add `matches`; `RfpEmailStatus` adds
  `extract`, `match`, `review_match`, `merged`, `duplicate`;
  `RFP_EMAIL_PENDING_STATUSES` adds `extract`, `match`; `RfpEmailCounts`
  adds `matches`; the list row type adds the new fields;
  `isRfpEmailMethodEditable`), and the backend `Tab` literal,
  `TAB_STATUSES` (`matches = (review_match,)`, `processed = (done, merged,
  duplicate)`), `_LIST_SELECT` (adds `extracted_project_name`,
  `extracted_gc_name`, `match_project_id`, `match_score`, `resolved_gc_id`,
  `possible_rebid_project_id`).
- i18n: every string in all six catalogs (ceb, en, fil, hi, sw, ur). No em
  dashes.

---

## 4. Data model (migration 0122)

Apply after 0121. Release order: apply 0122, then deploy; the old sweep
ignores `extract` and `match`, so rows sit untouched in between, and the
startup pass picks up anything the old code parked at `done` meanwhile.
Rolling the code back leaves rows at `extract` / `match` waiting until the
new code is redeployed; nothing is lost.

`rfp_emails`, new columns:

| column | notes |
|---|---|
| extracted_project_name, extracted_gc_name | text |
| extracted_bid_due_at | timestamptz |
| extracted_bid_due_has_time | boolean not null default false |
| extracted_bid_notes | text |
| extract_model, extract_prompt_version | text |
| extracted_at | timestamptz, stamped on every exit from extract |
| sibling_of_email_id | uuid references rfp_emails(id) on delete set null, indexed |
| resolved_gc_id | uuid references general_contractors(id) on delete set null |
| resolved_gc_contact_id | uuid references gc_contacts(id) on delete set null |
| gc_match_kind | text check in (contact, domain, name, human, sibling) |
| gc_match_score | numeric(4,3) |
| gc_candidates | jsonb not null default '[]' |
| match_candidates | jsonb not null default '[]' (capped top 5) |
| match_project_id | uuid references projects(id) on delete set null |
| match_score | numeric(4,3) |
| match_llm_model, match_llm_prompt_version | text |
| match_weights | jsonb (full resolved settings + scorer_version + auto_merge_enabled) |
| matched_at | timestamptz, stamped on every match route (updated_at is not usable: method correction and the later creation slice write to done rows) |
| match_review_decision | text check in (merge, duplicate, no_match) |
| match_review_by, match_review_at | uuid, timestamptz |
| match_review_agreed | boolean |
| excluded_project_ids | uuid[] not null default '{}' |
| possible_rebid_project_id | uuid references projects(id) on delete set null |
| possible_rebid_score | numeric(4,3) |

The status check constraint is dropped and re-added with the new vocabulary
(the 0120 constraint is inline and unnamed; the migration looks its name up
from `pg_constraint` on the `status` column). After the swap:
`update rfp_emails set status = 'extract', attempts = 0, last_error = null,
next_attempt_at = null where status = 'done' and extracted_at is null;`,
which means exactly "parked by the intake slice, never extracted" and is a
no-op on any later run. The new backend runs the same guarded UPDATE once
at startup, before the first sweep tick.

`rfp_project_matches`:

| column | notes |
|---|---|
| id | uuid pk |
| rfp_email_id | uuid not null references rfp_emails(id) on delete cascade |
| project_id | uuid not null references projects(id) on delete cascade |
| gc_id | uuid not null references general_contractors(id) on delete cascade |
| project_gc_id | uuid references project_gcs(id) on delete set null |
| kind | text not null check in (merged, duplicate) |
| gc_added | boolean not null default false |
| contact_selected_id | uuid references gc_contacts(id) on delete set null |
| sibling_of_email_id | uuid (informational) |
| score | numeric(4,3) |
| candidate_rank | int (null when the project was not in the stored list) |
| breakdown | jsonb: the chosen candidate's `breakdown` object merged with its `verdict`, `confidence` and `reasoning`; never `project_id`, `name`, `number` (the row's own columns) and never a date value |
| candidates | jsonb (a copy of the email's match_candidates at decision time) |
| weights | jsonb (the snapshot) |
| gc_match_kind, gc_match_score, authorization_kind, invitation_method, sender_address, auth_dmarc, auth_compauth | provenance copied from the email at decision time (`sender_address` is `rfp_emails.from_address`, lowercased) |
| decided_by | uuid references profiles(id) on delete set null; null = the system |
| decided_at | timestamptz not null default now(); the row's creation time, there is no separate created_at |
| acknowledged_by, acknowledged_at, acknowledge_reason | uuid (set null), timestamptz, text |
| unmerged_by, unmerged_at, unmerge_reason | uuid (set null), timestamptz, text |

Indexes, one comment each naming the read it serves (0121 style):
`(project_id, decided_at desc)` (the project modal, both dashboard counts
with `unmerged_at is null` as a row filter, the cascade); `(rfp_email_id,
decided_at desc)` (the resume path and the detail route's latest row);
unique `(rfp_email_id) where unmerged_at is null` (one open decision per
email, which is what the merge resume relies on); `(gc_id)` (FK convention).
RLS enabled and forced, deny by default.

`project_gcs.rfp_match_id uuid references rfp_project_matches(id) on delete
set null` (null = added by a person or at project creation), partial index
`(rfp_match_id) where rfp_match_id is not null`, column comment "link the
matcher created". The unique key alone cannot distinguish the system's own
link from a concurrent add after a crash; this column is the durable marker
the resume reads.

SQL function `remove_project_gc_unless_sent(p_link_id uuid, p_project_id
uuid, p_gc_id uuid, p_refuse_if_sent boolean) returns uuid`, as in 3.7,
called through the service-role client like `claim_llm_jobs`.

`projects`: `bid_notes text`, `is_rebid boolean not null default false`.

PostgREST schema reload after the DDL.

---

## 5. Concurrency and races

- All pipeline writes stay conditional updates on the expected status; the
  sweep never touches `review_match`. Cascade rows are each a conditional
  update on `status = duplicate`; a concurrent human action on one of them
  wins and that row is skipped.
- Lease renewal moves from every 20 rows to before every LLM call:
  `_process_email` takes a `renew` callback from `_sweep` and calls it
  immediately before the `classify`, `extract` and `match` steps (one row
  can make all three calls in a single pass); when renewal fails the row is
  left at its current status and the sweep returns. The 20-row renewal
  stays for the free steps. The lease length is
  `RFP_EMAIL_INGESTION_LEASE_SECONDS` (600) instead of twice the poll
  interval, validated at boot to be at least
  `llm_background_wait_seconds + self_hosted_llm_timeout_seconds` (180 +
  120 today), so the second production worker can never acquire the lease
  while the holder is inside a call. The third-party route counts too: the
  Anthropic and OpenAI clients are built with `third_party_llm_timeout_seconds`
  (120) and `third_party_llm_max_retries` (1) instead of the SDK defaults
  (600s, 2 retries), and the validator uses the larger of the self-hosted
  timeout and `third_party_llm_timeout_seconds x (max_retries + 1)` (240
  today, so 180 + 240 = 420 fits the 600 default). The first sweep after 0122 drains the
  backfilled rows over several ticks (200 per sweep); this is expected and
  is why per-call renewal lands before the migration runs.
- Merge writes the match row before the GC link; a resume finds the open row
  and finishes the remaining steps keyed on `project_gcs.rfp_match_id`. The
  `(project_id, gc_id)` unique key turns a concurrent human add into a
  duplicate outcome instead of a second row. Two humans merging the same
  email into different projects: the partial unique index admits one open
  row, and the second decision is refused while the first row is open (3.6
  step 2) rather than dismantling it. The match step itself finishes an
  open merged row before scoring (3.5), so a system merge that crashed
  before its email write cannot land elsewhere on the next tick.
- The bundle's paged reads (`_page_all`) order on a unique tiebreaker
  (`id`) after their sort key, so a range boundary never repeats or skips a
  row that ties on `internal_bid_at`, `name` or `created_at`.
- The match step scores against a bundle taken once per sweep. A GC added to
  a project mid-sweep is caught by the unique violation; a project created
  mid-sweep is the accepted in-flight case (two emails for the same new
  project in one sweep both land at `done`; creation is a later slice).
- Unmerge races a send, a mark-as-submitted, or a stuck-sending reclaim:
  `_block_if_sending` alone sees only `sending` and `mark_submitted` writes
  `sent` with no `sending` phase, so the entry check is only a pre-check.
  The guard that counts is `remove_gc_link(refuse_if_sent=True)`: the retire
  of unsent rows is the claim (mutually exclusive with the generated/failed
  to sending/sent claims by row lock), and the conditional delete refuses
  in one statement when a `sent` or `sending` row exists. Unmerge removes by
  link id, never by (project_id, gc_id), so a GC a person removed and
  re-added after the merge is never deleted. A concurrent ordinary DELETE
  loses to the `rfp_match_unmerge_required` guard.
- The New Bid check is advisory and fails open on any error or timeout; the
  number unique index remains the only hard guard.

---

## 6. Configuration

| env | default | meaning |
|---|---|---|
| RFP_MATCH_AUTO_MERGE_ENABLED | false | system may merge and mark duplicates without a person |
| RFP_MATCH_WEIGHT_NAME | 0.6 | |
| RFP_MATCH_WEIGHT_BID_DATE | 0.3 | |
| RFP_MATCH_WEIGHT_BID_NOTES | 0.1 | maximum notes bonus (notes are never in the denominator) |
| RFP_MATCH_NOTES_MIN | 0.5 | notes Dice below this adds nothing |
| RFP_MATCH_BID_DATE_TOLERANCE_DAYS | 3 | scores 1.0 inside this |
| RFP_MATCH_BID_DATE_FAR_DAYS | 14 | scores RFP_MATCH_DATE_SCORE_FAR inside this, 0 beyond |
| RFP_MATCH_DATE_SCORE_FAR | 0.5 | |
| RFP_MATCH_EXACT_TIME_BONUS | 0.1 | added to the total on an exact timestamp match |
| RFP_MATCH_CONFLICT_CAP | 0.4 | name score cap on a discriminator conflict |
| RFP_MATCH_CANDIDATE_WINDOW_DAYS | 30 | bid date not older than this |
| RFP_MATCH_AUTO_THRESHOLD | 0.85 | floor on the TOTAL score for a confident candidate |
| RFP_MATCH_REVIEW_THRESHOLD | 0.55 | |
| RFP_MATCH_NAME_MIN_AUTO | 0.8 | |
| RFP_MATCH_NAME_MIN_AUTO_NO_DATE | 0.9 | |
| RFP_MATCH_RUNNER_UP_GAP | 0.1 | best minus runner-up must exceed this for an automatic project match or GC resolution; smaller goes to review |
| RFP_MATCH_LLM_CONFIDENCE_THRESHOLD | 0.8 | |
| RFP_MATCH_MAX_CANDIDATES | 5 | kept per email and sent to the model |
| RFP_MATCH_GC_AUTO_THRESHOLD | 0.85 | |
| RFP_MATCH_REBID_LOOKBACK_DAYS | 365 | wider name-only lookup |
| RFP_MATCH_REBID_NAME_THRESHOLD | 0.85 | name-only floor for possible_rebid_project_id |
| RFP_MATCH_PRECREATE_THRESHOLD | 0.5 | New Bid check |
| RFP_MATCH_PRECREATE_WINDOW_DAYS | 60 | New Bid check, on internal_bid_at |
| RFP_MATCH_SIBLING_WINDOW_MINUTES | 10 | per-recipient sibling window; 0 disables |
| RFP_EMAIL_INGESTION_LEASE_SECONDS | 600 | sweep lease |
| OPENAI_RFP_EXTRACT_MODEL, SELF_HOSTED_RFP_EXTRACT_MODEL | empty = classify model | |
| OPENAI_RFP_MATCH_MODEL, SELF_HOSTED_RFP_MATCH_MODEL | empty = classify model | |

Boot validation (`@model_validator`, always enforced): weights non-negative
with a positive name plus date sum; every threshold, cap, bonus, gap and
score in [0, 1]; review threshold <= auto threshold; far days > tolerance
days; rebid lookback > candidate window; precreate window >= candidate
window; max candidates >= 1; windows and days positive; lease seconds >=
background wait plus self-hosted timeout.

---

## 7. API

Under `/rfp-emails` (review-queue roles unless noted):

| method and path | purpose |
|---|---|
| GET /rfp-emails?tab=matches | `review_match` rows, with `match_project` and `resolved_gc` attached; `match_score` null for roles outside `ACTUAL_BID_VIEWER_ROLES` |
| GET /rfp-emails/counts | adds `matches` |
| GET /rfp-emails/match-stats | `{confident: {pending, agreed, disagreed}}` |
| GET /rfp-emails/{id} | explicit column list (never `select("*")`), plus the extracted, GC and match fields, `candidates` (with project names), `match` (the latest match row, open or closed), `excluded_projects` (id, name, number, unmerge reason, who), `possible_rebid` (project + score); all through `redact_candidates` for non-viewer roles |
| POST /rfp-emails/{id}/match/merge {project_id, gc_id?} | human merge; 404 on a malformed id or unknown GC; 409 `rfp_match_gc_already_sent` on a `ProposalSendError` |
| POST /rfp-emails/{id}/match/duplicate {project_id} | human "already on project"; 404 on a malformed id; 409 `rfp_match_gc_already_sent` on a `ProposalSendError` |
| POST /rfp-emails/{id}/match/reject | not a match; 409 while an open merged row exists |
| POST /rfp-emails/{id}/match/gc {gc_id} | set the GC; 404 on a malformed or unknown GC |
| POST /rfp-emails/{id}/match/reopen {reason?} | done or duplicate back to review |
| POST /rfp-emails/{id}/match/unmerge {reason} | unmerge by email (Estimating Admin, Executive, IT Admin) |
| POST /rfp-emails/{id}/dismiss | unchanged; 409 on a review_match row |
| PATCH /rfp-emails/{id} {invitation_method} | now succeeds on rows at extract, match and every later status |

Under `/projects`:

| method and path | purpose |
|---|---|
| GET /projects/{id}/rfp-matches | internal roles; `email` block for review-queue roles only; `require_rfp_ingest` + bidding |
| POST /projects/{id}/rfp-matches/{match_id}/unmerge {reason} | Estimating Admin, Executive, IT Admin; same gates |
| POST /projects/{id}/rfp-matches/{match_id}/acknowledge {reason?} | writer roles; same gates |
| POST /projects/similar {name} | writer roles; bidding only; `{similar, possible_rebids}` |
| DELETE /projects/{id}/gcs/{gc_id} | unchanged, plus 409 `rfp_match_unmerge_required` on a system-added GC |

Error codes (`app/core/error_codes.py`, `docs/ERROR_CODES.md`):
`rfp_match_not_actionable` (409), `rfp_match_gc_required` (400),
`rfp_match_gc_not_on_project` (409), `rfp_match_gc_already_sent` (409),
`rfp_match_project_closed` (409), `rfp_match_project_excluded` (409),
`rfp_match_unmerge_required` (409).

---

## 8. Security notes

- Extracted facts are model output over attacker text: capped, schema
  checked, stored as plain text, rendered as text. They only ever move a row
  between states or add a GC link that only the unmerge path can remove.
- The match model sees the extracted facts as delimited untrusted text and
  the candidate list as trusted; its reasoning is truncated before storage
  and rendered as text. The verdict is an AND gate, never a lift: an
  injected "same" alone cannot merge; an injected "different" only parks the
  row at `done`, where the review screen still shows it and reopen exists.
- Automatic merges and duplicates additionally require a verified sender
  (aligned DMARC or compauth pass, and an address, domain or GC-domain
  authorization). Override rows always wait for a person. A GC resolved by
  name is labeled as such everywhere it appears.
- Auto-merge is off by default. A malicious email cannot merge itself into a
  project without the switch being on, a verified sender, a confident LLM
  verdict, and no closer runner-up, or a person clicking.
- `actual_bid_at` is redacted by role on every response. Candidate
  breakdowns store date kinds, never values; the two routes that return
  them apply `redact_candidates` for roles outside
  `ACTUAL_BID_VIEWER_ROLES`: drop the `actual` kind, and where the date
  sub-score came from the actual date null it (with `exact_time` and
  `closest_kind`), replace `total` with the name-only total (name plus
  `notes_bonus`, capped at 1.0) and null the enclosing row-level `score`
  and `match_score`, because a total that survived redaction together with
  the weights snapshot would let a reader invert the date bucket. The list
  route selects no candidates, so it cannot tell whether a row's
  `match_score` used the actual date and nulls it for those roles
  unconditionally. The similar-projects check scores name only and
  windows on `internal_bid_at` only, so no score, membership or sort order
  in that response is a function of the confidential date.
- Email content never leaves the review-queue role set: the project-side
  match route returns match metadata to every internal role and the email
  block only to `RFP_REVIEW_ROLES`.
- Every human action is audited once; system decisions are audited with
  `actor_id = null` and the provenance fields.

---

## 9. Testing plan

- Fakes first. The in-memory fakes must be extended before any test in this
  slice is written, because the gaps fail silently. In
  `tests/test_rfp_email_ingest.py` `_Query`: consume `_negate_next` in every
  filter method (at minimum `in_`, `eq`, `gte`), not only `is_`; teach
  `_or_matches` the nested `and(...)` group (recursive parse on balanced
  parentheses) and make an unparseable expression raise; add `delete()`,
  `lt`, `lte`, `neq`, `contains`; add a `FakeDB.unique` registry with
  `project_gcs: [("project_id", "gc_id")]` and make `insert` raise an
  exception whose text contains `23505` (the shape `_is_unique_violation`
  recognizes); add `rpc()` for `remove_project_gc_unless_sent`. A regression
  test proves the fake's `not_.in_` and `and(...)` handling: the candidate
  query excludes `declined`, `pm_only`, `cp_only` and the excluded ids, and
  includes a project whose only in-window date is `actual_bid_at`.
- Unit (`tests/test_rfp_match.py`): normalization, stop tokens and reference
  number stripping; name score on shortened, reordered, extended and
  misspelled names; a null or all-stop-token side returns 0.0 (not 1.0, not
  absent); the conflict cap on Phase 1 vs 2, Building A vs B, Package 3 vs
  4, and these no-cap pairs: "Fire Station 12 Remodel, Bid 26-104" vs "Fire
  Station 12 Remodel"; "6370 - Terminal 1 Elevator/Escalator Modifications"
  vs "Terminal 1 Elevator and Escalator Modifications"; "26.6.7096B - WPCSD
  New K-8 School (60% Budget)" vs "WPCSD New K-8 School"; "Sunrise
  Elementary Modernization 2026" vs "Sunrise Elementary Modernization ITB
  26-104"; "Phase II" vs "Phase 2" (no cap) and "Phase II" vs "Phase 3"
  (cap); "Fire Station 12" vs "Fire Station 27" (cap). Date score levels,
  the exact-time bonus (fires when the project timestamp carries seconds in
  the same minute, not when the email has no time), the zone table ("2:00
  PM PST" and "2:00 PM PDT" on a July date both equal 2:00 PM Pacific; "2:00
  PM EST" resolves to 18:00Z; unknown zone falls back to Pacific). The date
  monotonicity invariant over a grid of name scores with and without notes:
  total(n, date inside tolerance) >= total(n, no date), and total(n, exact)
  >= total(n, same day) >= total(n, 3 days apart). The notes invariant:
  total with notes >= total without; a low-Dice notes pair leaves the total
  unchanged. GC resolution by contact, domain, name, ambiguity. The routing
  matrix including auto-merge off, sender unverified, override, the
  runner-up gap ("Clark County Fire Station" vs "... 12" and "... 7" with
  same/0.9 on both goes to review), no name. `build_match_messages` with
  notes containing both marker strings and an instruction sentence
  (markers scrubbed, facts inside the block, candidates outside, notes cut
  at 500). `parse_verdicts` with a 300-word reasoning, confidence 10 and
  verdict "SAME!!" (20 words, 1.0, `unsure`). `redact_candidates`. A
  golden-score test over a fixed fixture set with a comment that a changed
  expected value means `SCORER_VERSION` must be bumped, and an assertion
  that the snapshot contains `scorer_version`. Sibling key: same subject a
  day apart is not a sibling; different attachment sizes are not; a
  different `authorization_kind` or `authorization_rule_id` is not. The
  leader rule: a decided copy on either side is followed (earliest received
  of several), an older undecided one is waited behind, a younger undecided
  one is not, a failed or flagged one is ignored, and a timestamp tie breaks
  the same way from both sides.
- Pipeline (`tests/test_rfp_email_ingest.py`): walk `done -> extract ->
  match` to each outcome; crash-resume at `extract` and `match`; model down
  waits at both steps without spending; a `match` `model_missing` while
  classify is healthy still advances a classify row in the same tick, and a
  `provider_down` snapshot gates all three; a wait at extract and at match
  pushes `next_attempt_at` (CAS on the step's status); attempts spent at
  classify do not carry into extract; unusable output once at extract
  retries with backoff, twice advances with nulls and `extracted_at` set;
  the same at match routes to `review_match`; a null name lands at `done`
  with `no_project_name` after GC resolution and no LLM call; the lease is
  renewed before each of classify, extract and match on a single row walked
  in one pass, and a failed renewal stops the sweep with the row left at its
  step. A failure after the model call (a `merge_email` that raises
  RuntimeError, a parse that raises at extract) leaves the row at its step
  with `attempts = 1` and `next_attempt_at` set, and fails at the cap. The
  bundle factories order on `id` last, and ties across a page boundary are
  drained once. Merge ordering: crash after the link insert and before the
  stamp resumes as `merged` with `gc_added = true`, not `duplicate`; a
  human add before the link insert gives `duplicate`; a lost step-6 race
  compensates (link removed, match row deleted, 409); the match step
  finishes an open merged row without a model call and refuses to reject
  while one exists; an open merge into another project refuses a merge or
  duplicate elsewhere and leaves the first link alone; a duplicate request
  on an open merged row closes it as superseded and inserts a duplicate; an
  unknown body GC writes nothing. Unmerge: refuses after `sent` and
  after `sending`, including a `sent` row that appears after the pre-check
  (RPC returns null, 409, nothing changed); removes by link id only; a
  crash after the link removal and after the stamp both finish on retry;
  excludes the project, resets attempts and returns the row to `match`; the
  re-run lands at `done`; a human merge back into the excluded project is
  refused; cascade closes sibling duplicates decided after the merge and
  returns them to `match`, leaves earlier ones alone. DELETE on a
  system-merged GC returns 409 for every writer role, including after a
  send; a GC that was on the project before any merge still deletes
  normally with the same `proposal_sends` and notification end state as
  today. Acknowledge clears `rfp_new_count` and keeps `rfp_merged_count`;
  refused after send; unmerge still allowed on an acknowledged row until
  sent; duplicate rows never raise either count. Reopen on `done` and on
  `duplicate` (closes the match row). Dismiss on `review_match` is 409 and
  leaves the row untouched; PATCH `invitation_method` succeeds at `extract`
  and `match`. Sibling follower of a merged leader lands at `duplicate`
  with no LLM call; of a pending leader waits; of a failed leader extracts.
  Arrival order: the younger copy swept first finishes the work, and the
  older copy arriving on a later tick follows it (to `done`, to `duplicate`
  with its match row, or to the leader's `created_project_id`) with the
  extract seam never called a second time; an undecided younger copy is not
  waited on; a decided copy beats an older undecided one; a copy authorized
  by another rule is a separate invitation. Chains: three copies in all six
  arrival orders and four copies in all 24, each asserting exactly one root,
  every follower pointing at THAT root, one extract call for the group, and
  every follower carrying `created_project_id` once the root creates; a
  pre-existing chain four deep links end to end; a cycle and an over-deep
  chain stop instead of spinning. The same-instant tie is driven through
  `_SWEEP_SELECT`-shaped rows, with the `created_at`-stripped row shown to
  rank wrong. At match: a copy parked at `match` follows a decided copy to
  `done` or to `duplicate` with no `rfp_match` call, never waits, and an
  open merge of its own is resumed first.
  Startup backfill moves `done` rows with null `extracted_at` and leaves
  match-decided `done` rows alone.
- Router (`tests/test_rfp_emails_router.py`, `tests/test_projects_*`): the
  route table and role gating; merge and duplicate refuse an excluded
  project (409); an engineer's detail response for a row past `match`
  contains no candidate `actual_bid_at` value and no date sub-score whose
  `closest_kind` was `actual`; the accountant on `GET
  /projects/{id}/rfp-matches` receives the match rows with `email = null`;
  `POST /projects/similar` with dates in the body returns byte-identical
  results to the same call without them, contains no date-derived field for
  a non-viewer role, omits a project whose `internal_bid_at` is older than
  the window even when its actual date is inside it, and returns
  `possible_rebids` for a year-old name match; the three `/projects` RFP
  routes 404 when `RFP_INGESTION_ENABLED` is off, and `GET /projects` issues
  no `rfp_project_matches` query then; merge by an Estimating Admin creates
  no notification; a system merge produces one deduped `rfp_match.merged`
  row per tick.
- FE: `next build` (the lint gate); `/projects/similar` 500, 429 and a
  timed-out request each still create the project without the modal.
- E2E on dev: the backfilled rows drain through extract and match against
  the dev project book; one hand-made project matching a real email's name
  and date; review a match by hand; merge one; unmerge it; watch the row
  re-run to `done`; one hand-written email whose bid notes carry an
  injection, checked to land at `review_match` or `done`, never `merged`.

---

## 10. Out of scope

- Creating projects from emails, and matching against in-flight emails and
  bid drafts (the creation slice).
- Writing extracted notes into `projects.bid_notes`.
- The provenance line on the proposal send confirmation modal.
- Hoisting `_load_rules` / `gc_domains` to the per-sweep bundle (optional
  follow-up; outcome unchanged because the learn-back covers rows flagged
  before a rule existed).
- Production migration and Railway variables (needs explicit approval).

---

## 11. Pure module API (`app/services/rfp_match.py`)

The contract the pipeline, the routers and the tests code against. All
functions are pure unless they take `sb`.

```
SCORER_VERSION: str                      # e.g. "rfp_match_scorer_v1"
EXTRACT_PROMPT_VERSION, MATCH_PROMPT_VERSION: str
EXTRACT_SCHEMA, MATCH_SCHEMA: dict       # JSON schemas for llm.complete_json
FEATURE_EXTRACT = "rfp_extract"; FEATURE_MATCH = "rfp_match"

normalize_project_name(text, *, project_number=None) -> str
normalize_gc_name(text) -> str                            # the 3.2 suffix and generic-word rule
EMAIL_START, EMAIL_END: str            # delimiters, moved here from rfp_email_ingest._EMAIL_START/_END
clamp_confidence(value) -> float; truncate_words(text, limit=20) -> str   # moved here from rfp_email_ingest,
    # which imports all four back so _CLASSIFY_SYSTEM, parse_classification and the existing tests are unchanged;
    # rfp_match.py imports nothing from rfp_email_ingest
name_score(a, b, *, a_number=None, b_number=None, settings) -> NameScore
    # NameScore(score, dice, containment, conflict: dict | None)
date_score(email_due, has_time, candidate_dates, settings) -> DateScore | None
    # candidate_dates: list of (kind, datetime | date); None when email_due is None
    # DateScore(score, exact_time: bool, closest_kind, dates_used: list[str])
notes_score(email_notes, project_notes) -> float | None
score_candidate(email_facts, project, settings) -> dict          # the breakdown
rank_candidates(email_facts, projects, settings, *, excluded_ids) -> list[dict]
    # top MAX_CANDIDATES entries {project_id, name, number, breakdown, verdict: None, ...}
rebid_lookup(name, projects, settings) -> tuple[str, float] | None
route(candidates, *, has_name, has_date, gc_resolved, gc_on_project, sender_verified,
      auto_merge, settings) -> Route
    # Route(status, flag_reason, best: dict | None)
resolve_gc(email, bundle, settings) -> GcResolution
    # GcResolution(gc_id, contact_id, kind, score, candidates: list[dict])
sibling_key(row) -> tuple; received_order(row) -> tuple; same_sibling(row, cand) -> bool
sibling_decided(cand) -> bool                                     # `created` needs a project id
sibling_candidates(row, candidate_rows, settings) -> list[dict]   # both directions, oldest first
choose_sibling_leader(row, candidate_rows, settings, *, waiting_statuses) -> dict | None
read_siblings(sb, row, window_minutes, *, select) -> list[dict]
    # the one query, both sides, scoped to the row's test_session_id, capped at
    # SIBLING_READ_CAP (200) with a warning when the page comes back full
parse_extraction(obj, received_at) -> ExtractedFacts
    # ExtractedFacts(project_name, gc_name, bid_due_at, has_time, bid_notes, reasoning)
build_extract_messages(row, settings) -> list[dict]
build_match_messages(facts, candidates_for_model, settings) -> list[dict]
parse_verdicts(obj, sent_indexes) -> dict[int, dict]
settings_snapshot(settings) -> dict
redact_candidates(obj, role) -> obj
candidate_query(sb, lo_iso, *, select) -> query          # the or-group builder (coalesce on the actual date)
precreate_query(sb, lo_iso, *, select) -> query          # POST /projects/similar: internal_bid_at only
pg_ts(dt) -> str                                          # Z-suffixed literal
```

---

## 12. Service action API (`app/services/rfp_email_ingest.py`)

The contract the routers code against. Every function takes the sync
Supabase client first and returns fresh rows the router can hand to
`_detail`. Refusals raise `RfpMatchError(code, message)` (a `LookupError`
subclass carrying an `ErrorCode` value) so a router maps it to 409 (or 400
for `rfp_match_gc_required`) with the `X-Error-Code` header, exactly as
`_conflict` does today; `proposal_send.ProposalSendError` from the removal
helper is mapped by the same routers to 409 `rfp_match_gc_already_sent`.

```
class RfpMatchError(LookupError): code: str

merge_email(sb, email_id, project_id, gc_id, actor_id | None, *, bundle=None) -> dict   # the email row
duplicate_email(sb, email_id, project_id, actor_id | None) -> dict
reject_match(sb, email_id, actor_id) -> dict
set_match_gc(sb, email_id, gc_id, actor_id) -> dict
reopen_match(sb, email_id, reason | None, actor_id) -> dict
unmerge(sb, match_id, reason, actor_id) -> dict                 # the closed match row
acknowledge_match(sb, match_id, reason | None, actor_id) -> dict
match_rows_for_project(sb, project_id) -> list[dict]           # raw rfp_project_matches rows, decided_at desc
latest_match_for_email(sb, email_id) -> dict | None
excluded_projects_for_email(sb, row) -> list[dict]             # {id, name, number, unmerge_reason, unmerged_by}
rfp_match_counts(sb, project_ids, post_send_ids) -> dict[str, dict]   # {project_id: {merged, history, new}}; {} when the RFP flag is off
match_stats(sb) -> dict                                        # {confident: {pending, agreed, disagreed}}
backfill_parked_rows(sb) -> int                                # the startup pass; returns rows moved
```

`proposal_send.py` additions: `remove_gc_link(project_id, gc_id, *,
link_id=None, refuse_if_sent=False, via_unmerge=False) -> bool` and
`block_if_sending(project_id, gc_id, *, include_sent=False)` (raises
`ProposalSendError(409)`), plus `project_gc_rows` gaining `rfp_match_id` and
`rfp_system_added`. `routers/projects.py` owns `_rfp_match_counts` (the
flag check and the post-send derivation) and calls `rfp_match_counts` for
the queries.
