# Phase 7 owner decisions (2026-10-30)

Prompt for Claude Code. Context: `docs/ASSISTANT_PHASE7_PLAN.md` (planned and reviewed 2026-10-29). The owner has now answered every question in section 14. Several answers override plan defaults and some of the plan's hard rules. Nothing is applied and nothing is built until the plan is updated to match this file.

## What to do with this file

1. Update `docs/ASSISTANT_PHASE7_PLAN.md` so it matches every decision below: section 14 (record each answer), the 2.2 defaults table, the hard rules, and every section named under "Plan changes". Do not leave text in the plan that contradicts this file.
2. Add a build log entry in `docs/ASSISTANT_BUILD_LOG.md` dated 2026-10-30: "Phase 7 owner decisions recorded", with a pointer to this file.
3. Re-run the adversarial review (same process as 15.2) on the changed areas only: external delivery (Teams and email), the send ledger, quiet hours for external sends, email-ID matching, the `rfq_inbox.py` change, the routing grid column. Log findings in 15.3.
4. Re-verify the migration number on disk and on dev before writing 0190 (take the next free number if 0190 is gone).
5. Bring the short list under "Still open" to the owner before WI-1 applies anything to dev. All other existing rules stay: dev project `bpidntbyvoooqvaispup` only, part B with the owner at the screen, no em dash anywhere.

## Guiding principle (owner)

The app owns notifications. The assistant does work and speaks up only when the app does not already cover something. The router must not duplicate what BDR already sends. Use this rule to resolve any detail this file does not settle, and note each such call in the plan.

## Out of scope: moved to Phase 8

Do not build any of this in Phase 7. Write it into the plan as "Deferred to Phase 8":

- Model triage. The assistant itself receives notifications (the Phase 7 record rows are its feed), decides what work to do, and decides whether to escalate to a person, whether or not the app already notified them.
- No per-person limit on how often the assistant may reach a person.
- No separate spending cap for triage. The existing company cap (`assistant_settings.global_daily_cap_usd`) would also stop triage; the owner will reconcile that in Phase 8.
- An inbox read tool for the chat assistant (question 20).

Phase 7 keeps the rule that the router calls no model.

## Plan changes that override hard rules or defaults

### A. The router now sends Teams and email (questions 2, 3, 5, 6)

- Replace the hard rule "The router sends no email and no Teams card" and deviations D1, D5 and P2.
- Router-owned high and urgent items: one Teams message to the person when delivered, plus the inbox row and in-app bell as planned.
- Digests (7:00 AM and 1:00 PM PT, workdays): one email to the person, plus the inbox digest and bell as planned. Content uses the same server templates (ids, enums, counts, Pacific times, project number). No free text, no amounts.
- Never re-send an app notification. Record rows never go to Teams or email.
- Exactly once per item and channel: add a send ledger (for example `assistant_external_sends`, unique on target and channel, with claim, lease, attempts, status, sent_at, error kind). Retry with backoff and dead-letter like events. Two workers must never send twice.
- Reuse the existing Teams and Graph email send helpers rather than building new senders. Respect the existing global kill switches (`NOTIFICATION_EMAILS_ENABLED`, `TEAMS_ALERTS_ENABLED`).
- Change the source-scan test: the router may send only through its own ledgered delivery path, and never through `notifications.deliver`, `notify_role`, `notify_roles` or `notify_user`.
- Quiet hours (6 PM to 6 AM PT) apply to the router only, including its Teams and email. BDR's existing email, Teams and due reminders are unchanged (question 3).
- Urgent items send Teams at any hour, including quiet hours (question 5).
- A high item held overnight on a workday folds into the 7:00 AM digest (email) if that digest has not gone out and it is before 10:00; otherwise it is sent at once, by Teams (question 5, option A; section 8.4 as written).
- On weekends and holidays, held high items are sent alone at 6:00 AM by Teams; there is no digest (question 6).
- `assistant.approval_expiring` may show in the app during quiet hours, with no Teams until quiet hours end (question 33).
- Live test: real sends only to the two e2e test accounts. Verify everything else with fakes.

### B. No caps (question 16)

- Remove the daily bell cap: the cap logic in `assistant_inbox_deliver` (A11.3), the `bell_cap` precedence, and `assistant_proactive_max_bells_per_day`.
- Remove the digest render limit: a digest lists every item. Remove `assistant_proactive_digest_max_items`.

### C. The 6:03 digest and the 7:00 AM digest are independent (question 8)

- Remove rules R1, R1a and R2 and the `await_6_03` hold. The 7:00 AM digest lists today's bids whether or not the 6:03 covered them. R3 becomes moot.
- The router no longer reads `due_digest_log`. Drop the matching golden cases and live-test step 9 checks. Keep R4 and R4b (due reminders) and the other rules.

### D. Late AI-extracted quotes are fixed in BDR, and the router's late-quote kind is dropped (questions 17 and 26)

- Edit `app/services/rfq_inbox.py` so an AI-extracted quote received after submission gets a proper `late_quote.received` bell for everyone, like the manual paths. `rfq_inbox.py` leaves the read-only list. Coordinate with the other builds that edit it.
- Drop the router-owned kind `quote.late` and rules R14, R15 and R15b.
- Drop the quotes tail: the `quotes` cursor row, `quotes_late_ai_idx`, and `quotes_tail` in the events source CHECK. The bell tail records the new late-quote bell as `bell.late_quote`.
- Router-owned kinds are now exactly three: `bid.due_not_ready`, `todo.due`, `assistant.approval_expiring`.

### E. Match alerts from one email by its IDs, in code (question 17)

- Replace the 120-second time bucket (D13, R13) with matching on the vendor email's message ID and conversation ID. Alerts that come from the same email collapse into one inbox item, with the others linked to it.
- This is deterministic code in the router. No model is involved.
- First verify where those IDs are stored today: quotes, RFQ reply rows, bell metadata, and the 0184 mailbox notices. Report what exists. If the `rfq_inbox.py` change in D needs to carry the IDs in bell metadata, do it there. Fall back to the time bucket only where no ID exists.

### F. Admin role defaults move to the routing grid (question 11)

- Add an "Assistant" column to `NotificationRoutingSettings.tsx` (TA-owned; tell the TA owner before editing). It leaves the read-only list.
- Have the column call the planned `/assistant/alerts/defaults` endpoints. Do not change the 0177 routing API.
- The company switch and the feedback report stay in the admin block on Settings > My assistant.

### G. Email labels (question 28)

- Add `assistant.inbox` and `assistant.digest` to `notification_email._TYPE_META`. `notification_email.py` leaves the read-only list for this edit only.

### H. Retention (question 15)

- Processed events: 90 days (was 30). Inbox router rows: 90 days. Digest and slot ledgers: 1 year. Add the send ledger: 90 days. Legal hold skips the person, as planned.

### I. Record rows (question 1)

- Keep record rows: every allowlisted BDR bell is copied quietly into the person's inbox. They never push, never go to Teams or email, and are not listed again in the digest beyond the plan's single "N other updates" count line. They are the feed for Phase 8 triage.

## All 34 answers

| # | Topic | Answer |
|---|---|---|
| 1 | Record rows | Yes, quiet; no repeat listing in the digest (see I) |
| 2 | Delivery | Router high and urgent items to Teams, digests by email; app notifications never re-sent (see A) |
| 3 | Quiet hours scope | Router only (a) |
| 4 | Fixed quiet hours | Fixed company hours, 6 PM to 6 AM PT, read-only (a) |
| 5 | Severity and 6 AM release | Option a; urgent goes to Teams at any hour |
| 6 | Digest days | Workdays only, skip holidays (a) |
| 7 | Open decision 10 | Suppress, not refuse; keep the stub (a) |
| 8 | 6:03 digest | Independent of the assistant digest; R1, R1a, R2 removed (see C) |
| 9 | Delegation | Not needed to receive items, for now (a) |
| 10 | Role defaults | In code, overrides in the table (a) |
| 11 | Where admins edit defaults | Assistant column in the routing grid (b, see F) |
| 12 | Auto-downgrade | Yes (a) |
| 13 | Project mute duration | Until unmuted or the project closes (a) |
| 14 | Snooze | Remind me next digest, max 3 per item (a) |
| 15 | Retention | Events 90 days, inbox 90 days, digests 1 year, legal hold respected (see H) |
| 16 | Flood caps | None (see B) |
| 17 | AI late quote | Fix BDR (b), plus email-ID matching in code (see D, E) |
| 18 | Bid date change kind | Deferred; the app covers it (a) |
| 19 | Mirror exclusions | Exclude (a) |
| 20 | Inbox read tool | Phase 8 |
| 21 | Accountants | Yes (a) |
| 22 | Feedback report | Yes, counts only (a) |
| 23 | Tab position | Inbox second (a) |
| 24 | Router bells and project_id | No project_id (a) |
| 25 | Legacy rows | Backfill and classify trigger (a) |
| 26 | MVP kinds | `bid.due_not_ready`, `todo.due`, `assistant.approval_expiring` (quote.late dropped) |
| 27 | Prod index build | Concurrently, by hand, before release (a) |
| 28 | `_TYPE_META` | Add both types (b, see G) |
| 29 | Digest catch-up | 3 hours (a) |
| 30 | Kind-by-role grid | Option a, without the quote.late row |
| 31 | Inbox and BDR bell state | The inbox never changes BDR bell state (a) |
| 32 | `mfa_reset` | Leave pending items alone (a) |
| 33 | Approval-expiring in quiet hours | In-app allowed; Teams waits (a) |
| 34 | Mute this project, minimal cut | Keep the plan as written, mute included (a) |

## Still open (bring to the owner before WI-1 applies anything)

1. A person with no Teams alerts set up (`teams_alert_users.enabled` false or missing): fall back to email, or in-app only?
2. Do router Teams and email follow each person's existing notification preferences, or always send?
3. Is the `rfq_inbox.py` fix (D) built inside Phase 7's waves, or as a separate change by the build that owns that file?
