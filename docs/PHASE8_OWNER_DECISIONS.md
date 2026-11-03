# Phase 8 owner decisions (2026-11-02)

Source: the owner's answers to the 28 questions in `docs/ASSISTANT_PHASE8_PLAN.md` section 19, given after a voice walkthrough of `docs/PHASE8_OWNER_QUESTIONS_BRIEF.md`. This file is the source of truth and overrides the plan wherever they differ. The plan must be updated to match before any build (gate G0a).

## Answers

| Q | Topic | Answer | Notes |
|---|---|---|---|
| 1 | What triage may do | (a) | Stage 1 only: none, suggest, escalate. No tools, no cards. Stage 2 not built. |
| 2 | Feed | (a) | Bell-sourced record rows only. |
| 3 | What the model may read | (a) | Facts only: ids, enums, counts, times, project number. Never message text or names. |
| 4 | Model | gpt-6-luna | NOT a plan option. API id `gpt-6-luna`. It needs a row in the app's price list (`app/services/llm_pricing.py`). OpenAI list price (developers.openai.com/api/docs/pricing, checked 2026-11-02): 0.10 USD per 1M input tokens, 0.01 USD per 1M cached input, 0.50 USD per 1M output; prompts above 272K input tokens bill the whole request at 2x input and cache rates and 1.5x output. |
| 5 | Batching | (a) | Per person, oldest row waits 5 minutes or 10 rows wait, up to 10 rows per call. |
| 6 | Escalation and "never duplicate" | (c) | No rules. The model's choice is final. |
| 7 | Who an escalation may reach | (a) | Only the person whose row it is. |
| 8 | Escalation urgency | Not a plan option | The model may choose urgent whenever it judges something urgent (digest, now/high, or urgent). |
| 9 | Quiet hours | (a) | Escalations follow the router's quiet hours; triage itself runs at any hour. Urgent breaks quiet hours per the Phase 7 rule. |
| 10 | Reach limit and fault fuse | (a) | No per-person limit, no same-project rule. Company-wide fuse (more than half of the last 50 decisions, at least 20, were escalations, or more than 30 escalations in one hour) pauses triage for everyone; cleared by an Executive or IT Admin turning the company switch off and on. |
| 11 | Triage spend versus the company cap | (b) | Pure sharing, no floor. IT Admins and Executives control the company switch. The Stage 2 part does not apply. |
| 12 | Whose budget | (b) | Company budget only. Triage spend does not count against any person's daily budget. |
| 13 | When the cap stops triage | (b) | Retry the skipped rows after midnight if they are under 24 hours old. |
| 14 | How triage is turned on | (b) | On for everyone who has the assistant. No personal opt-out. Company switch only. |
| 15 | Permission to act (delegation) | (c) | No delegation needed, like receiving Inbox items in Phase 7. |
| 16 | Chat inbox read tool | (a) | Deferred `inbox_list`, untrusted text, hides dismissed rows, never marks read, digests as a view. |
| 17 | Digest email opt-out | (a) | Per-person switch for the assistant digest email only. |
| 18 | `system` stamp on emails | (a) | New optional argument on `graph_email.send_mail`, refused inside an assistant run. |
| 19 | How escalations are labelled | (a) | Bells stay `system`; triage run id kept in triage tables and the Inbox row. |
| 20 | Re-checking merged alerts | (a) | Do not re-triage. |
| 21 | Retention | (a) | Decisions live as long as their Inbox row (90 days); run records 90 days; legal hold respected. |
| 22 | Live test | (a) | e2e accounts only, real model calls capped at 1 USD, real Teams only to e2e accounts, digest email to the owner's test address labelled as a test. |
| 23 | Accountants | (a) | May receive triage; suggestions never name write work their role cannot do. |
| 24 | Card lifetime | Not applicable | Question 1 is (a). |
| 25 | Migration number | (a), (b) fallback | 0196; if taken, the next free number, announced to bdr-94. |
| 26 | Order versus browser v2 | (a) | Build waves 0 and A once the plan is updated; wait for bdr-94's release before waves B to D and the firewall pairs. |
| 27 | Escalations held over a weekend or holiday | (a) | Delivered in the app only (bell and Inbox), no Teams. Urgent escalations are not held (they break quiet hours). |
| 28 | Overlap with the router's "Bid due and your part is missing" line | (c) | No rule. Both can reach the person. |
| Extra | Translations | English only | New Phase 8 strings ship in English only. |

## Owner notes for Claude Code (verbatim intent)

- With 6 (c) and 8 allowing urgent, the model can send Teams at any hour with no fact checks. This also makes the early-morning bid issue (brief section 3) go away.
- 12 needs a new company spending identity (triage spend is attributed to the company, not to a person).
- 14 and 15 remove the opt-in that gave triage permission.
- 22 needs a new way to cap the live test at 1 USD (the plan's floor and per-person budget no longer apply to triage).

## Consequences the plan update must carry

- Remove from the triage path: the fact predicates that turn an unsupported reason into `none`, router rules R27, R28, R31 and R32 for triage escalations, and the read-bell severity cap. Escalation text still comes from server templates with slots the router fills from facts (question 3 stays (a)), so no model text reaches a person.
- Remove the per-person opt-in column, the triage delegation column and the Alerts-card opt-in UI; keep the company switch, the env flag and (if the plan keeps one) the pilot allowlist as a rollout lever only; record whether the allowlist stays.
- Remove the triage floor (P11) and the per-person budget charge for triage; add a company spend identity for reservations and settlement; the company cap `assistant_settings.global_daily_cap_usd` still stops triage.
- Add the midnight retry for cap-skipped rows under 24 hours old.
- Add `gpt-6-luna` to the price list and the triage route, and redo the cost model with its prices.
- Keep: one escalation per record row (dedupe), the company fuse, quiet hours (urgent excepted), the person's own mute and "not useful" marks from Phase 7, offboarding, retention, the `inbox_list` tool, the digest email opt-out, the `system` stamp.
- Any point these answers leave ambiguous (for example whether R24, closed project, still suppresses a triage escalation under "no rules") is decided by the plan updater with the owner's guiding principle and listed as a short follow-up question; it does not block waves 0 and A unless it changes the migration.
