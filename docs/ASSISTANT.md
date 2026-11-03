# BDR Assistant: design record for per-user assistants

Current override, 2026-10-27, implementing the owner decision of 2026-10-26: Phase 5 uses the existing backend Microsoft application access only. No Connect Outlook, delegated permission, delegated app or mandatory separate Reader app. BDR assistant delegation grants remain mandatory and are not OAuth delegation. Current code contracts and activation gates are in [ASSISTANT_PHASE5_PLAN.md, sections 1 to 8](ASSISTANT_PHASE5_PLAN.md); verification is in [ASSISTANT_BUILD_LOG.md](ASSISTANT_BUILD_LOG.md). Local Phase 5 code is implemented, but designated live mail/calendar tests, a configured vetted web gateway and browser acceptance remain activation gates. No tenant or production change is authorized.

Phase 6 checkpoint, 2026-10-27: the personal-browser vault, owner settings/status UI and principal isolation foundation are implemented; migration 0186 is applied to dev only. Personal execution is hard-disabled in SQL, API, platform and worker. Phase 6 is not complete. See [ASSISTANT_PHASE6_PLAN.md](ASSISTANT_PHASE6_PLAN.md), especially section 8, for implemented boundaries and the next execution wave. No Windows/site activation or production rollout is claimed.

The current Email and calendar section and linked implementation record supersede older dated plans, UI placeholders and Appendix A. The original design status and historical decisions below are retained as history, including statements about work that had not been built in October 2026.


Status: design v2, 2026-10-21. Supersedes the v1 plan. Incorporates the owner's decisions of 2026-10-21, the 42 verified critic findings, and the second review round of the same day (35 findings: 1 blocker, 11 major, 23 minor), applied in place. Nothing here is built yet; migrations start at 0170 (0163 to 0169 are reserved by the AI Estimating autonomous build in progress on 2026-10-21; take the next free number at build time if that block grows) and apply to the dev Supabase project only, never BDR_Prod.

## Summary

Every internal user gets one Assistant: route /assistant, tables assistant_*, env ASSISTANT_ENABLED, package bdr_be/app/assistant/, secret ASSISTANT_TOKEN_SECRET. The word "agent" stays reserved for the Windows browser worker (core/agent_auth.py, browser_agent_* tables, /agents). The browser-agent nav label and Help topic are renamed "Browser jobs" (approved); the /agents route and browser_agent_* table names do not change.

The assistant is one more client of the existing FastAPI app. Every tool is a declared mapping to a real HTTP route, executed in-process through httpx.ASGITransport under a server-minted principal, so require_writer, the router-level require_feature 404s (main.py:658-660 and the mount table), _FIELD_EDITORS, redact_for_role (projects.py:178) and the rate limits apply with no per-tool authorization code. Because the backend uses the service-role key and RLS has no policies (0007, 0093), any tool that touches Supabase directly would be an authorization bypass; the registry refuses a ToolSpec without a route and CI refuses an import of get_supabase inside bdr_be/app/assistant/.

What changed since v1, driven by the owner: mail is in scope (own mailbox plus the shared bids@, office@ and adminassistant@ for everyone; every mailbox except a short exclusion list for Executive and IT Admin assistants, behind a confirm card with a human-typed reason and an append-only log); sends go from the user's own mailbox with a visible "Sent by assistant" marker; assistants may email other internal users; records render "<User>'s assistant" as the actor; Executive and IT Admin assistants get evidence tools and xlsx reports; the assistant may run AI Estimating behind a cost card; user management is excluded for every role; every write needs an approval click; replies are English only; external estimators get no assistant; IT Admin can read a user's conversations, with an audit trail.

What changed since v1, driven by the critics: the shim is only the ASGITransport target; approvals carry a precondition and execute as a queue job after a compare-and-set; the ledger has phase rows and never replays a begun write; taint covers notes, RFP text and names; the PATCH schema cannot carry Go/No-Go; the rate limiter keys on the principal; splitter spend is priced and capped; whats_due and plans tools go through gated routes; memory provenance is server-derived; first production release is roughly 9 to 10 weeks out.

What changed in this revision, driven by the researchers: the actor label is derived at render time instead of being stored (no actor_label column); the PM mailbox fix needs no migration, because ownership reuses profiles.email and profiles.rfp_mailboxes; the PM ingest mailbox needs Mail.Send as well as Mail.ReadWrite because submittal requests send from it; project page opens are already recorded in project_open_events (0117) and only need a read route; Appendix A is complete and ready for the owner to run.

What changed in the second review round (2026-10-21): the PM mailbox scoping fix moves to phase 0 as a standalone ticket that releases ahead of the assistant; the delegated mail tools get their own allow-set (connected UPN plus the shared set) that is never unfiltered, for every role; Connect Outlook binds the Microsoft account to the BDR profile and refuses any other account; taint is sticky on the conversation, not the turn; read_mailbox and every per-person evidence tool are a new sensitive_read tier with a human-typed reason; approval rows are immutable and the click carries the hash and precondition it rendered; memory updates on active rows become pending revisions; the browser tool gets one dedicated route that can only enqueue user-principal jobs; the Accountant's assistant may send email but cannot write a BDR record; projects, project_gcs, todos and bid_split_jobs join the 0171 provenance set; llm_pricing is a Python dict, not a table; the deny-list is written as exact (method, path_format) pairs; stale deps.py and config.py line citations are re-pointed.

Five stories ship first: what is due today and project status with citations; enter labor numbers; create a project; send files to the splitter and file them into a project; a 7 AM recurring report and "stop sending it". Mail, evidence, calendar, browser control and the proactive router follow in phases 4 to 7.

## Decisions log (owner answers, dated 2026-10-21)

1. Planner model: OpenAI only, pinned like the plans_* features, even when FULL_SELF_HOSTED_LLMS_ENABLED is on. Project data and mail go to OpenAI.
2. Executive and IT Admin assistants read every mailbox in the organization except the exclusion list (MAILBOX_EXCLUSIONS, default empty; the owner initials the final list in this log under open decision 2): a deliberate reversal of the 0134 rule for the assistant's read_mailbox tool. Whether the PM Emails page gives those two roles the same full view, minus the exclusion list, is open decision 18 (proposed: yes). rfp_email_visibility.py:17-20 stays as written for the RFP queue. Employees have been given notice.
3. The owner holds the Entra and Exchange admin rights; Appendix A is written for the owner to run after the build.
4. Every write to a BDR record and every send needs an approval click in v1. No standing approvals. Assistant housekeeping rows (assistant_memories, conversation state, schedule bookkeeping) are not BDR records; their rules are under Memory.
5. "Enter labor numbers" means the bidding labor panel (PUT /projects/{id}/labor); when ambiguous the assistant asks.
6. Sends go from the user's own mailbox with a visible "Sent by assistant" flag.
7. No automatic AI Estimating pass; the assistant may run AI Estimating behind a cost-confirm card.
8. Project creation: the assistant asks for missing information; it does not pull fields from an RFP email or a BuildingConnected invite.
9. External estimators get no assistant.
10. Internal name "assistant" and the "Browser jobs" rename approved.
11. Retention: defaults proposed here. IT Admin can read users' conversations.
12. One Windows PC is sufficient for now. Browser control will expand beyond Procore.
13. "No deletes" was too rash: allowlist plus effect tiers plus a deny-list, with approval gates.
14. User management is excluded for every role, including Executive and IT Admin; the assistant answers with a deep link to the users page.
15. Executives will use the assistant to understand and evaluate employees: project opens, when the estimator received it, file access events, metadata, and reports on these.
16. Assistants may email other internal users, not only self.
17. Reply language is English only; the no em dash rule applies to every reply, prompt, skill file and string.
18. "Acting as a user" means the assistant inside BDR, not the browser worker.
19. Certified payroll: aggregates only, no CP writes.
20. The PM mailbox hole in routers/emails.py is closed on the rfp_email_visibility pattern plus the three shared mailboxes, as a phase 0 standalone ticket that releases ahead of the assistant.
24. Phase 8 model triage (2026-11-02, owner answers to the 28 questions of docs/ASSISTANT_PHASE8_PLAN.md section 19, source of truth docs/PHASE8_OWNER_DECISIONS.md; migration 0196, parts A and B applied on dev (part B 2026-11-03, ledger 20261103002332 0196_assistant_phase8_b, including B6 assistant_triage_set_enabled), not on prod; changes Phase 3 deviation D4 a second time after entry 22): a separate triage loop (app/services/assistant_triage.py, outside the three model-free router modules) classifies the Phase 7 bell-sourced record rows of everyone who has the assistant while the company triage switch is on (Executive or IT Admin controls it; no personal opt-in or opt-out, no delegation). Stage 1 only: per row the model answers none, suggest or escalate from an enum-only schema, with no tools, no cards, no conversation and no memory read (Incognito by construction). It sees a fact pack of ids, enums, counts, Pacific times and the project number, never message text or names, and never reads or sends anything itself. Model gpt-6-luna (USD 0.10 per 1M input, 0.01 cached, 0.50 per 1M output; row in app/services/llm_pricing.py, feature assistant_triage). Batching per person: the oldest row waits 5 minutes or 10 rows wait, up to 10 rows per call. Code checks only that the answer is well formed; the model's choice is final (no fact predicates, no router rules R27, R28, R31, R32, no read-bell severity cap, no per-person reach limit). An escalation reaches only the person whose row it is, as one router-owned assistant.triage_escalation bell plus Teams, text from a server template filled with router-computed facts (never model text), severity normal, high or urgent as the model judges; it follows quiet hours except urgent, and is held to the in-app channels over a weekend or holiday unless urgent. One escalation per record row (dedupe); the person's own mute and not-useful marks from Phase 7 still apply; merged alerts are not re-triaged. A company-wide fuse (more than half of the last 50 decisions, at least 20, were escalations, or more than 30 escalations in one hour) pauses triage for everyone until an Executive or IT Admin turns the company switch off and on. Spend is the company's: a fixed company spend identity holds the reservations (kind triage), counted against assistant_settings.global_daily_cap_usd, no floor, no separate triage cap and nothing against any person's daily budget; rows the cap refused retry after Pacific midnight while under 24 hours old. Also shipped: the chat read tool inbox_list (deferred, untrusted text, hides dismissed rows, never marks read, digests as a view), a per-person switch for the assistant digest email only, a work-on-this prompt on Inbox items, and an optional actor_kind argument on graph_email.send_mail that accepts only system and is refused inside an assistant run, so router digest emails log as actor_kind system. Retention: decisions live as long as their Inbox row (90 days), run records 90 days, legal hold respected. New strings are English only. Accountants may receive triage and suggestions never name write work their role cannot do. Deviations from the design record and earlier phases (plan section 1.4): D1 a model runs unattended for triage (a second exception to Phase 3 D4 after entry 22; not a turn mode, so the three-mode pin in tests/test_assistant_no_model_unattended.py is unchanged, and the triage service joins that test's scanned modules); D2 unattended runs get no tools at all and park no card, where Permissions item 9 allows read tools plus notify_self and draft tools; speaking up is a router event and work moves to a turn the person starts; D3 an escalation may reach a person about something BDR already notified them of, an exception to Phase 7 decision A (never re-send an app notification); the escalation carries the assistant's own template, never the bell's content; D4 triage spend belongs to the company spend identity and the company cap, not to a person, with no floor (Permissions item 12 names only the per-user and company caps); D5 no delegation and no personal consent, so triage uses the router's eligibility test instead of access.owner_check; D6 a new per-person channel preference, the assistant digest email opt-out (Phase 7 P36 added none); D7 app/services/graph_email.py leaves the Phase 7 read-only list for one additive keyword-only argument; D8 the model reads the person's Inbox through a new model-facing route /assistant/tools/inbox while /assistant/inbox and /assistant/alerts stay denied to the model; D9 the router gains a fourth router-owned kind, beyond the three Phase 7 P26 fixed; D10 no per-person reach limit is built, where the Phase 7 plan called one a Phase 8 item; D11 the model may choose urgent, which Phase 7 never let a person or a default pick for a new kind, and which breaks quiet hours and goes to Teams at any hour. Detail and the rest of the contract: docs/ASSISTANT_PHASE8_PLAN.md.

## Architecture

### Model and transport

Register feature `assistant` in services/llm.py _FEATURES as openai_only. Do not build it by copying _plans_feature (llm.py:217), which carries switch=plans_reader_enabled; a test asserts the assistant feature is on when PLANS_READER_ENABLED is off. A boot validator refuses ASSISTANT_ENABLED when FULL_SELF_HOSTED_LLMS_ENABLED is on unless ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED=true, which the owner has authorized; the assistant joins llm_health.FEATURE_LABELS (llm_health.py:72) so the model-status bar shows where it is routed. A second feature `assistant_extract` is self-hosted-allowed and tool-less, for summarizing or classifying untrusted text on the Qwen boxes.

Add complete_messages() beside complete_parts (llm.py:1208). It takes a neutral message list (system, user, assistant, function_call, function_call_output and reasoning items), passes store=False, requests include=["reasoning.encrypted_content"], persists the returned reasoning items in assistant_messages.blocks alongside the function_call items, and replays them in order on the next round. It accepts function tools only, returns parsed tool_calls, and reuses _guarded (llm.py:802), the llm_gate slot, usage capture and llm_call_log. A phase 0 test runs a three-round tool loop under store=False. Nothing depends on previous_response_id (llm.py:1282-1283). Tool definitions are serialized with sorted keys in a byte-stable order per (flags, role) tuple and the per-user block goes last so OpenAI prefix caching works. Hosted tool_search and namespaces are not used; when the registry passes about 30 tools, a client-side find_tools(query) is added.

### Agent loop

A chat turn is an llm_jobs row of job_type assistant_turn with target_id = conversation_id; the partial unique index llm_jobs_active_target_uq (0094) gives one in-flight turn per conversation. The loop copies plans_reader_chat.py (MAX_ROUNDS at line 82, the loop at 866, _fit_round at 945): at most 8 rounds and 25 tool calls per turn, the last round offers no tools, a per-round character budget, a spend check before every call, every call logged. Progress is written to assistant_messages.tool_calls after each tool. Assistant jobs run in their own claim pass with capacity 2 (modeled on _claim_plans, llm_queue.py:815). LlmBusy is a short in-turn retry. _JobSpec (llm_queue.py:194) has no max_attempts field and enqueue hardcodes it (llm_queue.py:878); add an enqueue argument max_attempts_override. "Profile missing or inactive" is a permanent failure.

The worker runs job bodies through asyncio.to_thread. Calling the app through ASGITransport from that thread needs its own event loop: the executor is a plain def that runs asyncio.run(client.request(...)) on a dedicated loop per call, never a coroutine on the server loop (the sync-in-async mistake that froze prod before). Starlette BackgroundTasks run before ASGITransport returns, so a write that triggers background email (PATCH internal_bid_at, notes with @mentions) blocks the executor for the duration of the Graph sends. Every write tool is therefore wrapped in renew_lease (llm_queue.py:1066) unconditionally, and result_summary is built from the route's email_log rows, not the HTTP status alone. The client uses raise_app_exceptions=False so a route 500 becomes a recorded failed step. Before adding the claim pass and the scheduler loop, verify os.cpu_count() on the Railway box: main.py sets no executor and about 12 pollers plus every llm_jobs run already share the default thread pool.

### Principal

The worker mints a `bdra_` token: HMAC-signed with ASSISTANT_TOKEN_SECRET, 5-minute TTL, bound to (user_id, run_id, approval_id or null). core/deps.get_current_user (deps.py:79) gains one branch: a bdra_ token is accepted only when scope["bdr_internal"] is true; otherwise it is refused exactly like bdrw_ (deps.py:100). httpx 0.28.1 ASGITransport fills scope["client"] with a default address, so "no network client" is not a usable signal; the explicit marker is required. The branch loads the owner's profile as usual (is_active, role, email and rfp_mailboxes re-read every call), verifies an unrevoked assistant_delegations row for that user, forces is_dev False, ignores X-Impersonate-Estimator (deps.py:159-162), skips the aal2 check because authority comes from the delegation, checks that the token's (user_id, run_id) equals the provenance contextvar set by the shim (see Actor provenance details), and sets CurrentUser.via_assistant_run_id. "Acting as a user" throughout this document means this principal inside BDR, never the browser worker (decision 18). The actor label "<User>'s assistant" is never stored on CurrentUser; it is derived wherever an actor is rendered.

### The shim is never the served app

The ASGI shim lives in bdr_be/app/assistant/shim.py as a module-level callable that closes over app.main.app, sets scope["bdr_internal"] = True and enters the provenance context (core/provenance.acting_as_assistant) before awaiting the app. It is passed only to httpx.ASGITransport(app=shim). It is never assigned to app.main.app, never registered with add_middleware (main.py:336-353), and main.py never serves or registers it; main.py imports only app.routers.assistant, never app.assistant itself; that router imports only the pure assistant modules (access, repo, schemas, skills_index) at load time and the agent loop lazily inside its handlers, so importing app.main never loads app.assistant.loop, the executor or the shim (a subprocess test pins it), and a test walks app.main.app.user_middleware and app.main.app.router.routes to assert no AssistantShim is served. Dockerfile:33 keeps serving app.main:app. A boot assertion checks that app.main.app is a FastAPI instance, and a phase 0 test sends a valid bdra_ token through TestClient(app.main.app) and expects 401. The same shim enforces the firewall: method DELETE is refused outright, any request whose matched route is not in the registry allowlist is refused, and the admin deny-list is refused regardless of role. Matching resolves scope against app.main.app.router (Route.matches) and compares the matched route's path_format and methods to ToolSpec.route; deny-list entries are stored as (method, path_format), never raw strings.

## Permissions and safety

1. Live profile. The owner's profiles row is re-read on every tool call; deactivation and role changes bite on the next call. Dev powers never reach an assistant.
2. Delegation grant. assistant_delegations(user_id, created_session_aal check ('aal1','aal2') since 0171, created_at, expires_at, revoked_at, revoked_reason). Chat turns are created under the user's own aal2 JWT. Dev rule (0171): only while the backend runs with mfa_required False (dev and test, read from get_settings() on every call) an aal1 session may also create the grant, recorded as created_session_aal 'aal1'; with mfa_required True only aal2 is accepted, a refused send answers 403 mfa_step_up_required when the person has a factor and mfa_enrollment_required when they have none, and a live aal1 grant is revoked (reason mfa_required) and replaced at the next aal2 send. Schedules, continuations and approval executions run only while an unrevoked grant exists. reset-mfa (users.py:485), PATCH /users/{id} with is_active=false or a role change (users.py:596), and DELETE /users/{id} (users.py:744) all run the audited offboarding step described under Retention first.
3. Who gets one. INTERNAL_ROLES only (roles.py:36). External estimators are excluded. Accountants get an assistant that cannot write a BDR record: WRITER_ROLES excludes them, so every write_internal and expensive tool 403s through require_writer, and the browser tools declare roles=WRITER_ROLES on their ToolSpec. The Accountant's assistant may still send email from the Accountant's own mailbox to internal and external recipients under the same approval card as everyone else (decision 16; a send from the user's own mailbox is not a BDR write), with the actual-bid rule applied: recipient binding refuses non-ACTUAL_BID_VIEWER_ROLES recipients for any report or message carrying actual bid amounts, and the Accountant has no evidence tools. The Accountant is a row in the seven-role tool test. Open decision 19 lets the owner restrict the Accountant's assistant to notify_self instead.
4. Allowlist, then deny-list. Only routes declared by a ToolSpec may be called. The shim matches on path_format, so the deny-list is stored and written as exact (method, path_format) pairs; every pair below was verified against the router files on 2026-10-21. Never in the registry regardless of role: users (users.py): POST /users (invite, :331), POST /users/{user_id}/reinvite (:420), POST /users/{user_id}/reset-mfa (:485), POST /users/{user_id}/reset-password (:509), PATCH /users/me/role (:580), PATCH /users/{user_id} (:596), DELETE /users/{user_id} (:744). Browser agents: every write route under /browser-agents (browser_agents.py: jobs/{job_id}/retry :246 and cancel :241, help/{help_id}/* :296-329, every admin/* route :343-431 including admin/settings, admin/secrets/{name}, admin/types/{key}/probe, admin/canary and admin/stop-running) and every route under the worker protocol prefix in agent_worker.py (protocol.API_PREFIX); today there is no job-creation route at all, jobs come from system code (app/services/browser_agents/queue.py:168 enqueue), the admin canary and retry. Auto send (auto_send.py): PATCH /auto-send/settings/enabled (:61), PATCH /auto-send/settings/markup (:69), POST /projects/{project_id}/auto-send/send-now (:92), POST /projects/{project_id}/auto-send/accept (:110, 0159). AI spend: PATCH /ai-spend/cap (ai_spend.py:75), POST /ai-spend/reset (:84). llm_monitor: POST /llm-monitor/jobs/{job_id}/retry (:277) and cancel (:294). Training: PATCH /training/boq/{example_id}/review (:190), PATCH /training/bid-split/{example_id}/review (:284). Deleted projects: POST /deleted-projects/{archive_id}/restore (:109). Projects (projects.py): DELETE /projects/{project_id} (discard of a just-created project, :925), POST /projects/{project_id}/delete (the 0141 typed-name delete, :1034), POST /projects/{project_id}/merge-preview (:1093) and /merge (:1113), POST /projects/{project_id}/rfp-matches/{match_id}/unmerge (:2144), POST /projects/{project_id}/abandon (:1464), POST /projects/{project_id}/failed-to-bid (:1484). RFP matching: POST /rfp-emails/{email_id}/match/merge (rfp_emails.py:1588) and /match/unmerge (:1694). Mark submitted (0140, external_submission.py): POST /projects/{project_id}/external-submission (:68), /undo (:78), /requests/{request_id}/cancel (:103), grant (:114), deny (:125) and revoke (:139). Outcome: POST /projects/{project_id}/outcome (outcome.py:26). Verify commit: POST /projects/{project_id}/verify (pricing.py:592, commit_verify). Complete send-out: POST /projects/{project_id}/proposals/complete-send-out (proposals.py:598). Go/No-Go: POST /projects/{project_id}/gono/decide (gono.py:53) and /undo (:84). Estimator: POST /projects/{project_id}/assignments/{assignment_id}/revoke (estimator.py:715). RFQs: DELETE /rfqs/{rfq_id}/select (rfqs.py:1045). Payroll: POST /payroll/reports/{report_id}/finalize (payroll_reports.py:193), /submit (:201), DELETE /payroll/reports/{report_id} (:93), and every other write under the payroll_* routers (employees, employee documents, projects, rates, settings, cpr), since decision 19 allows no CP writes; there is no certify route. IT Admin Settings writes (the 0150 model box switch, RFP blocked and authorized senders, PM RFI sync and browser-agent settings) are added as pairs from the route table at build time, with a test that walks app.main.app.routes and fails when any route carrying an IT Admin only or dev only dependency is missing from the list. POST /projects/{project_id}/advance (workflow.py:43) is not denied: advance_lane reaches it with gono_action='review' hard-coded in the ToolSpec, because the shim cannot express an argument rule. A user-management request gets a deep link to /settings/users and one line of explanation.
5. Effect tiers, enforced in code. read: automatic. sensitive_read: read_mailbox, user_activity and every evidence tool whose filter is a user_id; a confirm card names the target mailbox or person and carries a reason the human types or edits, the tool runs on the click and writes nothing, and the log stores the human-entered reason plus the approval id. write_internal: approval card with a diff, every time in v1. external_send: any email, proposal, RFQ, nudge, RFI send, note with @mentions, PATCH internal_bid_at (projects.py:1279-1297 emails every internal user), calendar write, browser submit; approval every time, payload-hash bound, never standing. expensive: plans_read_pages, run_ai_estimating, split_files_into_project; cost or page preview, then approval. forbidden: the deny-list.
6. Approvals. Confirm tools never execute inside the turn. They write an assistant_approvals row with the exact payload, its sha256, a rendered preview and a precondition (the target row's updated_at, or a sha256 of before_snapshot when the table has none), and the turn ends with an action card. The click calls POST /assistant/approvals/{id}/approve under the user's own aal2 JWT and sends back the payload_sha256 and the precondition the card rendered (the server embeds both in the card when the message is served). The request does one conditional update: SET status='approved', decided_by, decided_at, decided_session_aal WHERE id=? AND status='pending' AND user_id=? AND expires_at > now() AND payload_sha256=? AND precondition=? (jsonb equality). That compare-and-set is the single-use guard across the two uvicorn workers, and because the hash and precondition the human saw are in the WHERE clause, a card still on screen cannot approve a row that changed underneath it. A pending row is never mutated: payload, payload_sha256, preview and precondition are immutable after insert (trigger assistant_approval_immutable), and status only moves forward. It then enqueues an llm_jobs assistant_approval_execute job targeting the approval id and returns 202; the panel polls. The approval_execute job re-reads the target row; when the precondition no longer matches it sets the row to expired with reason changed_since_preview, inserts a new pending row with a new id and a fresh diff, and rings the bell. Otherwise it re-hashes the payload and executes exactly the one route named in the approval row through the shim with a bdra_ token bound to (user_id, run_id, approval_id), so provenance and the firewall apply exactly as to turns. The approval_execute job never calls the model and holds no tool list. set_labor_numbers re-runs GET plus merge at approve time. write_internal approvals expire in 1 hour, external_send and expensive in 4 hours, drafts in 24 hours; approvals whose project moved to submitted, abandoned or verified expire automatically. The model cannot approve, and page or email text cannot approve.
7. Exactly-once at step level. assistant_tool_calls is append-only with a phase column ('begun', 'completed', 'failed') and UNIQUE(idempotency_key, phase). RPC assistant_tool_call_begin inserts the begun row with lease_owner or returns the existing rows on conflict; a completion insert carries after_snapshot, result_summary, ok, error_kind and duration_ms and is rejected by a fence check when lease_owner does not match. A requeued turn (the sweep requeues interrupted jobs, llm_queue.py:1329-1360) replays the transcript and treats a step as done only when a completed row exists. A begun-without-completed read is re-run. A begun-without-completed write is never auto-replayed: the turn parks as waiting_review with a "this step may have partially run" card showing before_snapshot against the live row. create_project passes a client idempotency token stored in projects.assistant_idempotency_key (nullable, unique where not null), so a replay finds the row instead of creating a second project.
8. Creates are irreversible through the assistant. Revert covers every other write from before_snapshot through the same route and approval path. A project created by the assistant shows a "Discard in BDR" link on its Activity row; the human discards in the UI.
9. Unattended runs (schedules, and the model continuation assistant_continue that resumes a turn after a split job finishes) get read tools plus notify_self and draft tools only. Any write parks an approval card and rings the bell. The approval_execute job of item 6 is not a continuation: it runs no model, offers no tools and may write only through the one route named in its approval row.
10. Sub-app flags. Each ToolSpec carries sub_app and the toolset is built from features.enabled_map(user.role) (features.py:84). whats_due, notes, files and analytics tools are tagged bidding because /projects/today carries _BIDDING_ONLY (projects.py:624) and those routers mount under _BIDDING (main.py:772-774). Pending approvals whose mapped route now 404s move to expired with reason feature_off and a bell notice; waiting_external tasks move to failed with reason feature_off after one sweep; paused_feature_off schedules resume with a "resumed" notice when the flag returns.
11. Certified payroll. Tools return server-redacted aggregates even for CP writers (SSN, bank, wage-detail and employee-document fields stripped before the model sees them). No CP writes.
12. Rate limits and spend. core/ratelimit.py keys buckets on (scope, user_id) in a per-process dict (ratelimit.py:35, 49). Inside _check, when the principal carries via_assistant_run_id the key becomes (scope, user_id + ':assistant') with its own lower limits, and check_outbound_email (ratelimit.py:208) does the same, so the human and the assistant never share a bucket; a test asserts that an assistant 429 leaves the human's quota unchanged. The per-turn cap of 25 tool calls and DB-backed daily caps are the cross-worker control. Spend: per-user daily cap (default 5 USD) and global cap (default 50 USD), enforced by reserving an estimated cost row at call start and reconciling on completion (the ai_spend.check_cap shape, ai_spend.py:309; ai_spend has no reserve-and-reconcile to reuse, so the reservation is new code) so in-flight calls on both workers are visible. services/llm_pricing.py PRICES (a Python dict at llm_pricing.py:98; there is no llm_pricing table, 0152:30 documents the dict) already holds gpt-6-astra, gpt-5.4, gpt-5.4-mini and gpt-5.4-nano plus the Anthropic rows claude-opus-5-5, claude-sonnet-5-5 and claude-opus-5 (claude_bid_split_model, config.py:278), so splitter and assistant costs are never NULL; split jobs enqueued by an assistant run count toward the user's cap, with a per-turn ceiling of 10 files or 400 pages on split_files_into_project. run_ai_estimating charges both the AI Estimating cap and the user's cap: the card shows the estimate next to the user's remaining daily cap and the global remainder, no card is offered when the estimate exceeds either remainder (the assistant says so instead), the AI Estimating pipeline's own check_cap still stops a run that passes the cap mid-run, and the assistant's reservation is released when the run is cancelled or fails and reconciled to actual cost on completion. Per-user outbound send cap of 20 per day in phase 5.
13. Provenance. created_by and actor FKs stay the human for integrity. actor_kind and assistant_run_id columns (see Data model and Actor provenance details) make the record render "<User>'s assistant" everywhere an actor is shown; the label is derived at render time from the human's current full_name, never stored.
14. Isolation. Every assistant_* query filters by user_id taken from the session or the job owner, never from tool arguments; a test asserts every function in bdr_be/app/assistant/repo.py takes user_id first and no assistant_* table is queried elsewhere. IT Admin access to another user's conversations is a separate, logged path (see Retention and offboarding).
15. Kill switches. ASSISTANT_ENABLED env, assistant_settings.enabled, assistant_profiles.enabled (IT Admin editable), disabled_tools in assistant_settings. GET /features (main.py:569) computes the assistant key per user: ASSISTANT_ENABLED and settings.enabled and profile.enabled and role in INTERNAL_ROLES, since enabled_map is role-only and cannot express a per-user switch.

## Prompt injection and taint

The assistant holds all three legs of the lethal trifecta: private data, untrusted content, and outbound channels. The controls are structural, not prompt text.

Taint sources. A turn is tainted the moment any tool result contains text authored by humans or by ingestion: email bodies and subjects (own, shared and cross-mailbox), attachments, file text, plans notes, web or browser content, project notes (estimator_notes is writable by the external Estimator role on assigned projects, notes.py:274), project name, notes, bid_notes, project_information and trade_instructions (RFP and BuildingConnected derived), GC, vendor and contact names, and file names. plans_reader_chat already treats every document, file and project name as data; the assistant keeps that rule across every tool. Note rows carry the author's role; estimator-authored notes are quarantined from default context and returned only through get_estimator_notes, whose result is tainted and labeled.

Consequences of taint. Every write or send tool for the rest of the turn requires a fresh approval even when the user's message asked for it. Memory writes land in pending_review and cannot be pinned. Recipient and URL binding strips any address, contact or URL that first appeared in a tainted result and flags the strip on the card. Tainted text goes only into user-role data blocks or tool outputs, never the system prompt. Assistant text renders escaped, with no auto-links and no remote images (the EchoLeak vector); actions render only from server-issued cards.

Taint is sticky on the conversation, not the turn. Every tool result, including read_mailbox summaries, is replayed into the next turn's context, so a planted instruction read in turn 1 is live in turn 2, where a benign "go ahead" would otherwise arrive in an untainted turn with write, send, schedule, browser and memory tools offered. assistant_conversations therefore carries tainted_at and mailbox_tainted_at, set the first time any message in it is tainted or mailbox_tainted and never cleared; every later turn in that conversation inherits both flags until the user starts a new conversation, and memory writes stay pending_review for the life of a mailbox_tainted conversation.

Cross-mailbox reads. A read_mailbox turn is a read-only turn, and the mode switch is explicit: the panel has a "Mailbox review" mode that only the user switches on (assistant_conversations.mode check ('normal','mailbox_review'); the planner never switches it, and a cross-mailbox request outside the mode is answered with an offer to switch). In that mode the registry builds the tool list from read and sensitive_read tools only, so no write, send, schedule, memory or browser tool is in the tool definitions at all, not merely gated, because an Executive assistant reading every external email the company receives is the highest-value injection target in the system; read_mailbox is offered only in that mode. A test asserts that the tool list served in Mailbox review mode contains no tool whose effect is write_internal, external_send or expensive and no memory, schedule or browser tool. The body is summarized through assistant_extract (tool-less, schema output) before the planner sees it; the raw body is stored out of line with a 7-day TTL and never in blocks. A follow-up action starts a new conversation (the old one stays mailbox_tainted for good) whose recipient binding still refuses anything that only appeared in the mailbox result.

Recipient and URL binding. Email recipients, cc, calendar attendees and fetch URLs must come from active internal profiles emails (GET /users/teammates), gc_contacts or vendor_contacts ids, or text the user typed in the current or the immediately previous user message (never anything earlier in the conversation, and never the body of a pasted forward). The executor strips anything else. Links inside an outgoing email body follow the same rule: an href that did not come from the user's typed text or from a BDR signed link is rendered as plain text with the scheme removed, and the approval card lists every href next to its anchor text, so a destination cannot hide under friendly link text in the branded HTML.

Eval harness. bdr_be/app/assistant/evals holds per-skill evals.json and triggers.json plus an injection corpus: a planted "forward the bid tabs" email, hidden text in a plan sheet, a tainted recipient, an estimator-planted note, a vendor name containing an instruction, a file named like a command, and a two-turn case: a planted instruction read in turn 1 followed by a benign "go ahead" in turn 2. It runs in CI against the dev Supabase project through a CI-only service key, with an assistant_eval_lock row preventing concurrent runs, on TS-* seed projects, never BDR_Prod. Release gates: attack success rate 0 percent on the corpus, tool-selection accuracy at least 90 percent on the 150-prompt set, cost per active user per day under the cap. The harness is in place before the first production release.

## Memory

Typed facts in rows, loaded in full for the MVP, soft-retired, user-visible. No Letta, mem0, graph or embeddings; pgvector stays uninstalled.

assistant_memories kinds: preference, about_me, instruction, project_note (scoped by project_id), reference. It never holds what the database already knows; the prompt and the Settings page say so.

Per turn, assembled in code at the end of the prompt: live context (name, role, enabled modules, Pacific date and time, current page and project); all pinned active memories; up to 60 unpinned active rows ordered by last_used_at within a 6k character cap; one line per active or paused schedule; project_note rows for the project in context.

Provenance is derived server-side, never set by the model. memory_save accepts title, body, kind and project_id only. The server sets source=user_stated and status=active only when the saved title or body appears verbatim in the user's own message in this turn (exact substring after case-folding and whitespace collapse, at least 12 characters), or when the user clicks Save in the toast; everything else is pending_review. pinned is set only by the user in Settings. Tainted-turn writes are forced to pending_review and cannot be pinned; when the user clicks Save in the toast during a tainted turn, the click is the approval, the row becomes active and stays unpinnable until the user pins it in Settings. Memory writes are blocked outright in any Mailbox review conversation and stay pending_review for the life of a mailbox_tainted conversation (taint is sticky, see Prompt injection and taint). Every row records origin_tainted, and tainted-origin rows are placed in the prompt as data blocks, never as instructions. memory_save runs a trigram near-duplicate check (similarity > 0.5 on title, pg_trgm per 0072) and returns candidates. memory_update(id, expected_updated_at) never changes the live body of an active row: it writes a pending revision (assistant_memory_revisions: memory_id, title, body, origin_tainted, created_at, decided_at) that the user approves in Settings or through the toast, and the old body stays live until then; pinned rows can only be edited by the user in Settings, so a planted instruction cannot rewrite a memory that is loaded into every prompt. memory_forget archives. Decision 4 covers BDR records and sends, not these housekeeping rows. A server-side regex filter rejects credentials, SSNs, routing numbers and payroll wage patterns. Caps enforced by trigger: 300 active, 40 pinned, 6000 pinned chars. Every change writes assistant_memory_events.

User control: Settings > My assistant > Memory lists rows by kind with edit, pin, archive, restore, approve pending and permanent delete (user only, with redaction cascading into event payloads). Incognito runs a turn that neither reads nor writes memory. Answers that used a memory show a "used memory" chip. A nightly step archives rows unused for 180 days and project_notes on lost, abandoned or deleted projects, and the merge route moves project_notes to the merge target. Phase 4 adds memory_search over tsvector plus trigram.

## Skills and tools

Registry at bdr_be/app/assistant/registry.py. ToolSpec(name, namespace, sub_app, route, method, effect, confirm, schema, response_trimmer, progress_label, roles). The registry refuses a ToolSpec without a route. Every tool supports response_format concise|detailed and limit, caps at 4k tokens, returns names plus short ids, and truncates with a narrowing hint.

MVP tools (phases 0 to 3):

- core: app_search (new GET /projects/search, paginated, declared in projects.py above the GET /{project_id} route at line 1140 next to /today and /next-number so it is not shadowed, plus /gcs and /vendors), get_project_context (GET /projects/{id}, stage-events, last 5 non-estimator notes, about 400 tokens), get_estimator_notes (tainted), list_teammates, load_skill, memory and schedule tools.
- bidding: whats_due (new GET /projects/due-checks under the bidding mount wrapping due_digest.compute_checks at due_digest.py:104 plus GET /projects/today), get_labor, set_labor_numbers (GET then PUT /projects/{id}/labor merged, because pricing.py:456-470 upserts the whole row; the card warns when workflow.maybe_reopen_verify_after_edit at workflow.py:750 will bounce Verify), update_project_fields (PATCH /projects/{id} through an AssistantProjectUpdate schema that strips the nine Go/No-Go enums, current_stage and every RFP intake field; a test asserts an assistant PATCH never changes current_stage or writes a gono decision, since projects.py:1300 calls _settle_rfp_intake which can finalize a go through gono.auto_go_if_scored at gono.py:302), add_project_gc (POST /projects/{id}/gcs), create_project_draft (POST /projects/similar then POST /projects, every ProjectCreate field at schemas.py:394 collected from the user), split_files_into_project (new POST /projects/{id}/split-drop, expensive tier with page-count preview, gated require_writer plus require_project_assignment exactly like the route it lifts), attach_split_job_to_project (new POST /projects/{id}/attach-split-job over rfp_split.promote_split_file at rfp_split.py:1696, write_internal tier: the card shows the target project and the file list, and its provenance set is bid_split_jobs plus project_files; for the "split first, choose the project later" case), splitter_status (exposes the splitter flag, page cap and handoff lock so the assistant can warn before uploading), todo_create and todo_complete (never DELETE /todos/{id}, todos.py:161).

Phase 4 adds: quotes_summary, run_report over the analytics GETs with xlsx links, the coded recipes estimator_vs_bid and quote_readiness built from the owner's documented rules, plans_get_brief, plans_list_documents, plans_search_notes, plans_get_output, plans_read_pages (expensive, queue job, estimate from plans_reader_images), run_ai_estimating (POST /ai-estimating/runs plus files plus start, estimate from plans_reader_pack.estimate_run, approval card showing the estimate against the remaining caps and refused when the estimate exceeds either, no automatic pass), advance_lane with gono_action='review' hard-coded (routers/workflow.py:148 auto-decides otherwise), pm_status, rfi_list, submittal_status, billing_summary, cp_week_summary and cp_report_status (aggregates), and the evidence tools. Every plans tool is a route under the plans_reader router so its three gates apply (routers/plans_reader.py:82: require_plans_reader, require_internal, require_ai_estimating; prod AI_ESTIMATING_STATE=test admits only Executive and IT Admin). plans_search_notes is a route over the new plans_reader_notes table, not SQL from the assistant package. Phase 5 adds comms; phase 6 adds browser tools.

Skills live at bdr_be/app/assistant/skills/<name>/SKILL.md with Agent Skills frontmatter (name, description, metadata.requires_flags, roles, version). Only name and description sit in the prompt; the body loads through load_skill; a few inject deterministically on keywords. MVP set: bdr-glossary, answering-status-questions, entering-labor-numbers, creating-projects, splitting-bid-files, scheduling-recurring-reports, managing-memory. Phase 4 adds answering-plan-questions (brief, search notes, category output, then paid pages behind a cost confirm, with the bid_split_segments.page_meta fallback when no run exists and a stale flag when project_files.created_at > run.started_at), building-reports and explaining-what-happened. Phase 5 adds drafting-and-sending-email. Every reply, card, skill file and string passes an em dash guard, and the system prompt fixes the reply language to English regardless of the user's UI locale (decision 17). The ASCII guard in openai_text._rewrite_acceptable (openai_text.py:60-67) stays on outbound vendor email bodies only.

Tasks. assistant_tasks gains a kind 'instruction' created from the To-dos page or from chat; a task runs as the owner with an approval card before any write. Cross-user assignment is an open decision (recommended: owner's permissions, assigner notified).

## Scheduling and proactivity

assistant_schedules and assistant_schedule_runs with UNIQUE(schedule_id, scheduled_for), claimed by RPC claim_due_assistant_schedules(n) (FOR UPDATE SKIP LOCKED, advances next_run_at in the same statement, inserts the run ON CONFLICT DO NOTHING, SECURITY INVOKER with search_path pinned per 0145). One polling loop started in the main.py lifespan under ASSISTANT_SCHEDULER_ENABLED and added to the shutdown cancel tuple; correctness lives in Postgres so both workers may run it. python-dateutil is declared explicitly in pyproject.toml (today only transitive) for RRULE expansion in America/Los_Angeles.

MVP schedules are recipe-only: due_today, due_this_week, missing_quotes, labor_not_entered, reusing the due_digest query and renderer with zero model calls. Creation requires a Confirm card with the next three run times in Pacific; nothing is saved before the click. Interval minimum 1 hour, cap 10 active per user, both enforced by trigger. "Weekday" means Monday to Friday excluding a G3 holiday list in assistant_settings; an ambiguous time ("at 7") is asked back; a nonexistent DST local time runs at the next valid instant and a repeated one runs once. Catch-up window 3 hours, overlap Skip, pause after 3 consecutive failures or when the owner is inactive, demoted or the required sub-app is off. "Stop sending that report" is schedule_end (status ended, history kept). only_if_changed plus last_result_hash records a silent no_change run; a project watch auto-ends when the bid is submitted or abandoned. Auto-pause after 10 unread deliveries. The Routines tab offers Run now and dry-run Preview. Delivery: chat, chat plus bell, or chat plus bell plus email to self through graph_email with a claim ledger, never the fire-and-forget notification mirror. Until phase 7, a recipe that overlaps the 6:03 digest or due_reminders is created with a notice that the system version will also arrive, and a precedence stub suppresses the recipe on days the digest already went to that user.

Proactive alerting is phase 7, but the schema lands in phase 3: assistant_events (outbox with dedupe_key), assistant_subscriptions (role defaults), assistant_inbox (UNIQUE(user_id, dedupe_key), severity, why, feedback). The router is deterministic, writes one inbox row per user, batches normal items into 7 AM and 1 PM PT digests, respects quiet hours 6 PM to 6 AM PT, and consults a precedence table against due_reminders, due_digest, calling_in and auto_send. No assistant ever messages another user's assistant; the cross-user channels are email from the user's own mailbox under an approval card and system notifications from the router.

## Email and calendar

### Mail scope, owner decision 2026-10-26

Application access only. Reuse the existing BDR backend application's Microsoft Graph credentials and existing application permissions. Do not create a delegated application, request delegated permissions, require Connect Outlook, or mandate a separate Reader registration. The earlier three-app design and Appendix A commands are historical and superseded. No tenant or production changes are authorized by this decision.

BDR assistant delegation is the existing human grant in assistant_delegations, not Microsoft OAuth delegation. Every mail operation requires a fresh active internal profile, an enabled assistant, an unrevoked unexpired BDR assistant delegation, live role and feature gates, plus the operation-specific authorization below. Application tokens may have broader reach than the person. Token availability and Microsoft accepting a request never establish the person's BDR authorization.

The existing backend app also serves ingestion, RFQ, proposal and other mail flows. Any future Exchange scope change must preserve those flows plus assistant reads of internal users' own mailboxes, configured shared mailboxes, approved Executive/IT Admin review targets and approved sends from each internal user's own mailbox. Do not execute the old scope-down to bids@/ingest/submittal senders: it would remove mailboxes this design now requires. Existing application credentials remain server-only. A separately scoped Reader app is a possible future defense-in-depth decision, not a current requirement or automatic provisioning task.

### Reading

Normal tools are email_search(mailbox, query, window), email_read(mailbox, id), and email_attachment_to_staging. Their finite allow-set is `{normalized live profiles.email} | normalized live profiles.rfp_mailboxes | SHARED_MAILBOXES`, for every internal role including Executive and IT Admin. The admin-maintained rfp_mailboxes mappings grant read authority and must be reloaded on every call; only authorized user administration may change them. Do not expand normal access using model arguments, historical OAuth rows, PM full-view scopes, or the backend app's actual reach. Unknown, malformed and out-of-scope targets produce byte-identical refusal before Graph. A request for a primary email without a usable mailbox fails closed; no fallback mailbox is chosen. An explicitly requested admin-mapped mailbox remains subject to its own live allow-set check.

Build `/users/{mailbox}/...` Graph paths server-side with encoded mailbox/message identifiers and bounded query options. Validate pagination against the original mailbox and operation, not merely the Graph origin. Recheck live BDR authority before each request. Subjects, senders, bodies and attachment names are untrusted data. Keep full body and attachment text out of durable transcript copies: ordinary raw results expire after 30 days, review raw results after 7 days. Only bounded summaries and metadata enter blocks. Attachment staging retains phase-2 ownership, PDF validation and staging limits, with an additional 20 MB mail attachment cap. Human-only owner-bound PDF previews use a fresh 60-second signed link and a forced-PDF blob in the existing modal.

read_mailbox(target_upn, reason) remains a separate sensitive_read path for Executive and IT Admin only. It uses the same existing application credential, but never the normal read authorization function. Require a server-backed Mailbox review conversation, a single-use immutable approval naming the exact target/window, and the human's 10-to-500-character reason. Recheck live role, mode, delegation, exclusion list, approval and database quotas immediately before access. Refuse every target in MAILBOX_EXCLUSIONS, default empty per owner decision. Caps remain 3 distinct targets per turn and 10 accesses per reader per Pacific day. Persist append-only assistant_mailbox_access metadata, human reason, approval and outcome before releasing content; failure to log releases no content. The first access to a target each week bells the reviewing owner. IT Admin has the access-log view, and the weekly digest goes to Executives and IT Admin.

First-target-per-Pacific-week notices, exclusion-change notices and count-only weekly digests are implemented as atomically deduplicated assistant inbox plus bell delivery to all active Executive and IT Admin reviewers. There is no distinct configured reviewing-owner recipient, automatic digest email or unattended model call. IT Admin alone gets the mailbox/evidence access log UI; Executive receives notices/digests without other-user conversation inspection.

The review conversation is permanently mailbox_tainted, read-only and memory-blocked. Summarize raw bodies through tool-less assistant_extract before exposing them to the planner; never fall back to raw text on extraction failure. A follow-up action requires a new normal conversation without automatically copying review raw data or summaries. MAILBOX_EXCLUSIONS is enforced in BDR. Do not claim Exchange exclusion-tag protection or a successful tag probe when no corresponding application scope exists. Exclusion changes remain audited and notified; future tenant-level scoping must be separately designed and authorized.

### Sending

Send only from normalized live profiles.email using application-authenticated `POST /users/{sender}/sendMail`, with saveToSentItems=true. The sender is never a model field, shared-mailbox choice, MS_SENDER fallback, alias, or historical OAuth UPN. The immutable external_send approval snapshots the sender and current authority preconditions; a profile remap expires/replaces the card. Recheck current profile, role, BDR delegation, recipient/URL bindings, attachment ownership, approval and database send cap before dispatch. Accountant may send under the same approval rule; external estimators may not.

All internal and external sends require a human approval click. Recipients come from authorized current BDR contact/profile records or exact addresses typed in the current/immediately previous user message outside quoted/forwarded content. Body hrefs require the same typed provenance or a server-issued BDR artifact link. Current conservative disclosure policy restricts all recipients to active internal ACTUAL_BID_VIEWER_ROLES when the source conversation is tainted, any attachment is present, or bid amounts are detected. This is stricter than the earlier bid-pattern-only proposal because free text and attachments cannot be reliably classified as free of actual bid amounts. Bound external recipients remain possible for untainted attachment-free messages without detected bid amounts. The preview shows To/Cc, internal roles, subject/body, every remaining href and anchor, attachments, sender and Reply-To, and the three markers.

The markers remain a server-generated visible footer `Sent by <User>'s assistant in BDR, approved by <User> on <date>`, one opaque X-BDR-Assistant send UUID, and the Outlook category `Sent by assistant`. Set Reply-To to the same live sender. Inline attachments total under 3 MB; larger approved artifacts use short-lived BDR links. Apply existing test-bench redirection and record effective delivery. email_log records human sent_by plus assistant provenance, approval_id and assistant_send_id.

Claim each approval once in a durable send ledger and reserve the 20-per-user Pacific-day send quota before network dispatch. Graph 202 means accepted, not delivered. An ambiguous timeout or crash after claim is uncertain and never automatically retried. A claimed in-flight request may complete after an authority change; unclaimed later requests must fail the fresh gate.

### Calendar

Calendar remains read-only, opt-in and application-authenticated through the same backend app. Do not assume the existing mail permissions include calendar permission. If current application permissions cannot read the user's calendar, report unavailable and keep polling disabled; do not create a delegated flow or request a grant automatically. When enabled, poll the user's calendarView every 10 minutes over a 3-hour horizon. The implementation uses Graph's UTC default without a Pacific Standard Time Prefer header, preserving unambiguous DST instants; unexpected offset-free non-UTC results are rejected. BDR-context events remain the default, every-event reminders are a user preference, and reminder lead defaults to 15 minutes bounded 5 to 180. Enabling performs a bounded capability check; 401/403 keeps/disables reminders. Deterministic matching and durable inbox/bell dedupe need no unattended model call or email send. The same live identity and BDR delegation gates apply. No calendar writes.

### Current Phase 5 web and UI contracts

The vetted gateway adapter is implemented, but no provider is configured by this build. [ASSISTANT_WEB_GATEWAY.md](ASSISTANT_WEB_GATEWAY.md) specifies the authenticated search/fetch JSON interface, typed query/URL provenance, public HTTPS IP pinning, refusal of redirects and bounded cited/tainted output. The gateway needs operator review and negative-destination validation before activation; there is no arbitrary direct HTTP fallback.

GET /assistant/outlook returns application configuration metadata `{available,authentication:'application',mailbox,shared_mailboxes}`, not proof of Microsoft reach. Settings show organization-managed access and calendar preferences. Conversation creation explicitly accepts normal/mailbox_review and returns immutable server mode plus mailbox taint. Review starts a new conversation; returning to normal starts fresh without copied text or summaries. Email approval previews render escaped sender/To/Cc/Reply-To/body, inert href destinations, markers and owner-authorized PDF previews, with accepted/uncertain/refused outcomes. IT Admin log pages show metadata/reasons/exclusions; the audited read/export API requires owner_id, purpose support/legal/offboarding/other and reason 10 to 500 characters. Mailbox-tainted content and raw tool results are excluded unless legal purpose. Owners see viewer/action/date notices without private reasons.

These code paths have offline checks, not completed live activation. The existing app's actual own/shared/review mail and calendar reach, a designated live sender/recipient, vetted gateway configuration and browser acceptance remain pending. Browser QA could not run: no enabled in-app browser was available and the isolated fresh-profile headless renderer was denied by the local sandbox. No successful screenshots are claimed.

### PM mailbox scoping fix

routers/emails.py (ingested_emails) has no mailbox scoping: GET /emails (emails.py:76), /unknown (112), /{email_id} (177), attachment download (203), assign (254) and unassign (274) carry only require_internal or require_writer, so every internal role, Accountant included, reads the PM mailbox, which in the dev .env is the owner's personal mailbox; PM_ENABLED defaults to true (config.py:148), so this is live wherever the PM module is. The fix is a phase 0 standalone ticket, PM mailbox scoping, with no dependency on assistant code, released ahead of the assistant and before any assistant tool touches ingested_emails: a pure module services/mailbox_visibility.py on the rfp_email_visibility shape (visible_mailboxes at rfp_email_visibility.py:73, apply_scope at 90, assert_visible at 126). Executive and IT Admin see every mailbox except the exclusion list (decision 2; the page itself is open decision 18, proposed yes), with a mandatory audit row on every read outside their own and shared set. Every other internal role sees rows whose mailbox is in the user's own set, profiles.email plus profiles.rfp_mailboxes, plus the three shared mailboxes from a new SHARED_MAILBOXES setting (default bids@, office@, adminassistant@), applied to every list, count and detail route with the identical 404 for "not yours" and "missing". A dev account wearing any other role is scoped exactly like that role, as in 0134. No migration is needed: ownership reuses the two existing profile columns and ingested_emails.mailbox already exists. The details follow.

#### PM mailbox scoping fix details

Owner decisions (2026-10-21): all internal users have access to the bids, office and admin assistant mailboxes; the fix should work like RFP ingestion, where users see their own mailbox; Executives and IT Admins may look into everything, on purpose, and employees have been given notice.

The vulnerability (verified):

- routers/emails.py scopes reads only with require_internal (emails.py:83, 118, 178 and 207) and writes with require_writer (258 and 277). No handler filters on mailbox: list_project_emails (emails.py:89-94); list_unknown_emails, including the whole-table pending and failed counts (emails.py:124-152); _fetch_email (emails.py:163-174), which get_email, assign_email and unassign_email use; download_attachment (emails.py:203-251), which never loads the parent email before minting the signed URL and only pairs attachment_id with email_id (emails.py:211-218).
- The router is mounted under the PM flag only (main.py:787).
- ingested_emails.mailbox is a single text not null column (0061_email_ingestion.sql:21), lowercased at ingest (email_ingest.py:147). Graph ids are per mailbox, so the same message seen in two mailboxes becomes two rows.
- Exactly one mailbox is polled: EMAIL_INGEST_MAILBOX (config.py:730). In the dev .env it is the owner's personal mailbox, so today every internal user, including the read-only Accountant, can read the owner's mail and attachments through PM Emails. The prod Railway value is not recorded in this repository or in this document; the email-ingestion memory note says the same personal address "now, shared mailbox later, env-only switch". Read it from Railway and write it here before the ticket ships.
- profiles has email text not null unique (0002_core_tables.sql:17) and rfp_mailboxes text[] (0134_rfp_email_visibility.sql:10-11). There is no UPN column. CurrentUser already carries both email and the normalized rfp_mailboxes (deps.py:54, 71-76, 179-183).

Rule:

| Viewer | Sees ingested_emails rows whose mailbox is in (this table applies to ingested_emails rows only; normal assistant mail tools use their own finite allow-set, see Reading) |
|---|---|
| executive, it_admin (any account, dev or not) | everything except the exclusion list (settings.mailbox_exclusion_set), a person's own mailbox always included; a mandatory audit row on every detail or attachment open outside own plus shared |
| estimating_admin, estimating_engineer_materials, estimating_engineer_labor | own mailboxes plus the shared list |
| accountant | own mailboxes plus the shared list (see the note below) |
| a dev account wearing any other role | scoped exactly like that role (same as 0134) |
| estimator | never reaches the router (require_internal) |

- Own mailboxes = profiles.email plus profiles.rfp_mailboxes, lowercased. email covers people whose login is their mailbox. rfp_mailboxes covers aliases, for example a profile email that differs from the mailbox address; it is already admin-editable, validated (models/schemas.py:136) and audited as user.rfp_mailboxes_changed (users.py:732). So no new column and no migration are needed for ownership.
- The editor label changes from "RFP mailboxes" (locales/en/translation.json:1999) to "Mailboxes", with helper text saying the list now covers both RFP and PM email (components/EditUserModal.tsx).
- Accountant: the computed brief said "shared only". The Accountant's own mailbox is their own mail, so the recommended default is own plus shared. If the owner wants shared only, it is a one-line change (drop ACCOUNTANT from _OWN_MAILBOX_ROLES). Open decision.
- Shared list: the new setting shared_mailboxes: str = "bids@g3electrical.com,office@g3electrical.com,adminassistant@g3electrical.com" (env SHARED_MAILBOXES), with a lowercased set property shared_mailbox_set modeled on rfp_email_ingestion_shared_mailbox_set (config.py:1225-1233). The same setting feeds the shared half of the assistant's normal self/shared allow-set (Reading). A second setting mailbox_exclusions: str = "" (env MAILBOX_EXCLUSIONS, default empty) with mailbox_exclusion_set is the one BDR source for the exclusion list, read here by the full-view path and later by read_mailbox; the owner edits it in Railway. Confirm the exact address of the admin assistant mailbox with the owner. Recommend setting RFP_EMAIL_INGESTION_SHARED_MAILBOXES (config.py:781, default bids@ only) to the same three addresses, env only, so RFP and PM agree.
- Executive and IT Admin full view reverses the 0134 "not micromanagers" stance for PM email only, minus the exclusion list, and is inferred from the assistant read-all decision rather than decided: it is open decision 18. The RFP queue keeps its rule unless the owner asks otherwise. audit("email.viewed_other_mailbox", entity ingested_email, payload mailbox and email_id) is mandatory, not optional, whenever a full-view role opens a detail row or downloads an attachment outside its own and shared set, so the human-UI path carries the same record as read_mailbox.

Code. New pure module app/services/mailbox_visibility.py. It does no IO, and it scopes ingested_emails rows only. The assistant's normal mail tools do not call it: they use the live profile email, admin-maintained rfp_mailboxes mappings and shared set, never an unfiltered scope. The application token's broad reach makes the dedicated BDR authorization check mandatory before Graph.

```python
from dataclasses import dataclass
from fastapi import HTTPException, status
from app.core.config import get_settings
from app.core.roles import Role
from app.services.rfp_email_visibility import _literal_safe  # or move it to a shared util

EMAIL_NOT_FOUND = "Email not found"   # byte-identical to emails.py:173
FULL_VIEW_ROLES = frozenset({Role.EXECUTIVE, Role.IT_ADMIN})
_OWN_MAILBOX_ROLES = frozenset({Role.ESTIMATING_ADMIN, Role.ESTIMATING_ENGINEER_MATERIALS,
                                Role.ESTIMATING_ENGINEER_LABOR, Role.ACCOUNTANT})

@dataclass(frozen=True)
class Scope:
    allowed: frozenset[str] | None   # None = every mailbox except `excluded` (full-view roles only)
    excluded: frozenset[str]         # always applied, full-view roles included
    own: frozenset[str]              # own plus shared, used for the audit decision

def shared_mailboxes() -> set[str]:
    return set(get_settings().shared_mailbox_set)

def excluded_mailboxes() -> set[str]:
    return set(get_settings().mailbox_exclusion_set)

def visible_mailboxes(user) -> Scope:
    own: set[str] = set()
    if user.role in _OWN_MAILBOX_ROLES or user.role in FULL_VIEW_ROLES:
        own = {m.strip().lower() for m in (user.email, *(user.rfp_mailboxes or ()))
               if isinstance(m, str) and m.strip()}
    own_and_shared = frozenset(own | shared_mailboxes())
    excluded = frozenset(m for m in excluded_mailboxes() if m not in own)  # a person always sees their own mailbox
    if user.role in FULL_VIEW_ROLES:
        return Scope(allowed=None, excluded=excluded, own=own_and_shared)
    return Scope(allowed=frozenset(own_and_shared - excluded), excluded=excluded, own=own_and_shared)

def apply_scope(query, user):
    scope = visible_mailboxes(user)
    if scope.allowed is not None:
        return query.in_("mailbox", sorted(m for m in scope.allowed if _literal_safe(m)))
    for m in sorted(scope.excluded):
        if _literal_safe(m):
            query = query.neq("mailbox", m)
    return query

def row_visible(row, user) -> bool:
    scope = visible_mailboxes(user)
    mb = (row.get("mailbox") or "").strip().lower()
    if not mb or not _literal_safe(mb) or mb in scope.excluded:
        return False
    return scope.allowed is None or mb in scope.allowed

def is_other_mailbox(row, user) -> bool:
    """True when a full-view role reads outside its own and shared set. The caller audits."""
    mb = (row.get("mailbox") or "").strip().lower()
    return user.role in FULL_VIEW_ROLES and mb not in visible_mailboxes(user).own

def assert_visible(row, user) -> None:
    if not row_visible(row, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, EMAIL_NOT_FOUND)
```

in_ and neq are safe for the single-valued mailbox column. postgrest-py quotes each value through sanitize_param, and _literal_safe (rfp_email_visibility.py:57) is the second line of defense, as in 0134.

Router changes (routers/emails.py). Handlers stay plain def, and the helper does no IO, so the sync SDK never runs inside async def.

- list_project_emails (emails.py:89-94): if visible_mailboxes(user).allowed is the empty set, return the empty envelope; otherwise query = apply_scope(query, user) before the search filter, so total counts only visible rows.
- list_unknown_emails: apply the same scope to the row query (124-129) and to both counts (138-152). Today the counts expose whole-mailbox volume.
- _fetch_email(email_id, user) (163-174): after the fetch, call assert_visible(row, user), then audit("email.viewed_other_mailbox") when is_other_mailbox(row, user), unconditionally. get_email, assign_email and unassign_email pass user. Retro-assign stays inside the same (mailbox, conversation_id), so an assign can never write across mailboxes.
- download_attachment (203-251): first load the parent with select("id, mailbox, project_id"), call assert_visible and the same mandatory audit for a full-view read outside own plus shared, then look up the attachment. Reuse that project_id for file_access.record, which removes the second lookup at 229-241. A refusal mints no signed URL and records no access.
- Other readers of ingested_emails are either dev-only or workers and do not change: rfp_ingest.recent_emails is behind require_dev (routers/rfp_ingest.py:253); pm_rfi_sync and rfp_test are workers. components/pm/PmSubmittalsPanel.tsx:625 already uses .catch(() => null) on /emails/{id}, so a scoped 404 degrades cleanly.

Migration: none is required, which is what lets this ticket release ahead of 0170. Optional, only if the scoped Unknown pool is slow: create index if not exists ingested_emails_mailbox_msg_idx on ingested_emails (mailbox, message_at desc), on the dev project only; if it is wanted before 0170 it takes the next free migration number at the time, otherwise it rides in 0171. The existing (mailbox, conversation_id) index (0061:58) already leads with mailbox.

Operational consequence to decide before shipping. EMAIL_INGEST_MAILBOX is the owner's personal mailbox in dev. After this fix, PM Emails and the Unknown pool show that mail only to the owner, Executives and IT Admins. Engineers and the Estimating Admin will see nothing until PM ingestion polls a shared mailbox. Submittal requests send from, and thread replies into, the ingest mailbox when SUBMITTAL_SENDER is empty (config.py:1038-1046), so submittal responses would also vanish for them. Options: (a) point EMAIL_INGEST_MAILBOX and SUBMITTAL_SENDER at a shared mailbox, for example office@, which then needs the BDR-APP-MAIL and BDR-APP-SEND tags in Appendix A; (b) add multi-mailbox polling, for which cursor and lease keys already embed the mailbox (0061:14-17). In both cases, make sure the owner's profile email or rfp_mailboxes includes the address that is polled. This is open decision 13, settled inside this ticket rather than allowed to block it: the recommended default is (a), and if the owner has not chosen by the time the scoping code is ready, the ticket ships with the scope in place and the owner's profile covering the polled address, and (a) or (b) follows as its own change.

Tests:

- tests/test_mailbox_visibility.py, mirroring tests/test_rfp_email_visibility.py: Executive and IT Admin, dev or not, get allowed=None with the exclusion set applied, an excluded mailbox is invisible to an Executive unless it is their own, and apply_scope emits one neq per excluded address for them; a dev wearing Estimating Admin is scoped; each other internal role sees exactly own email plus rfp_mailboxes plus shared, and cannot see an executive's mailbox; Accountant per the decision; values are normalized for case and whitespace; an unsafe value is dropped from the query and does not make a row visible; a row with a blank mailbox is invisible to a scoped viewer; the refusal body is byte-identical to the unknown-id 404 ("Email not found"); apply_scope sends one sorted in_ filter, and nothing for full-view roles.
- tests/test_emails_router_scoping.py: handlers called as plain functions against an in-memory fake Supabase, the pattern of tests/test_rfp_emails_router.py. Project list and Unknown pool return only scoped rows, and total, pending_count and failed_count are scoped; get_email on another person's mailbox gives a 404 identical to a random uuid; download_attachment on another mailbox gives a 404 with no signed URL and no file_access_events row; assign and unassign on another mailbox give a 404 with no write; Executive and IT Admin see every mailbox except the excluded ones, and exactly one email.viewed_other_mailbox audit row is written per detail or attachment open outside own plus shared and none for own or shared rows; the route table still puts exactly one of require_internal or require_writer on every /emails route.

## Browser control and credentials

The browser platform (0157, 0158) is dev-only, GET-only, untested on Windows and organization-wide in every dimension. Per-user tasks wait until the RFI browser agent is live in prod (phase 6). The design is fixed now and is generic across sites, because browser control will expand beyond Procore (decision 12); one Windows PC is sufficient for now.

Principal. browser_agent_jobs gains principal_kind check ('org','user') default 'org', principal_user_id and user_credential_id, with a check that user jobs carry a principal, set server-side at enqueue from the session, never from model output. browser_agent_types gains principal_kind; org types can only be enqueued by system code or IT Admin. The assistant reaches the platform through exactly one route, POST /assistant/browser-tasks in routers/assistant.py, which sets principal_kind='user' and principal_user_id from the session unconditionally, accepts only browser_agent_types rows with principal_kind='user', and refuses any other type name; every existing browser-agent write route and the whole worker protocol are on the deny-list (Permissions and safety, item 4), and there is no other job-creation route today (jobs come from system code through app/services/browser_agents/queue.py:168, the admin canary and retry). A test asserts that an IT Admin bdra_ principal cannot create an org-principal job by any path.

Credentials. assistant_credentials(user_id, service, label, origins text[], username, wrapped_dek, ciphertext, nonce, key_id, version, fingerprint, totp_ciphertext opt-in, status, UNIQUE(user_id, service)) is envelope-encrypted under a separate USER_CRED_KEK with AAD 'bdr-user-cred|user_id|cred_id|version'. service is a key from a code-fixed site registry (procore, buildingconnected, smartbid, more as signed worker releases add ceilings); each entry carries exact https origins, a login routine and a read ceiling. assistant_browser_sessions holds per-user, per-origin encrypted cookie jars (rfp_harvest_sessions.cookies is plaintext). Microsoft 365 and SSO passwords are refused.

Routes and isolation. /agent-worker/v1/user-identities/* resolves only credentials where owner = principal_user_id and the page's top-level origin matches a registered origin; the org routes /identities/procore/* return 403 for user-principal jobs. No tool, prompt variable or RPC lets a user job name an org identity. A second WinSW service under a different Windows account (bdr-user-agent) with its own bdrw_ token and allowed_types runs the user pool on the single approved PC. Help and MFA route to the owner via scope key login:u-<id>-<service>, never to Estimating Admins. Per-user spend and concurrency caps; redaction.load_secrets (app/services/browser_agents/redaction.py:250) adds job-scoped user secrets. When the PC is offline a task parks for up to 8 hours, then expires with a bell and an email. The BROWSER_AGENTS_ENABLED boot validator is decoupled from the PM RFI sync. User tasks stay GET-only; form submits come later behind payload-bound approval cards. Screenshots, steps and transcripts are owner-only, with a logged IT Admin override. "Control a computer" beyond a browser is out of scope.

## Evidence and reports for executives

The owner wants Executive and IT Admin assistants to explain why an event happened with deeper evidence and to pull reports (decision 15). Every evidence tool is a route under the analytics router (bidding mount, prefix /analytics/evidence) gated by a new EVIDENCE_ROLES = {EXECUTIVE, IT_ADMIN} and goes through the shim like any other tool; nothing reads tables directly, the model never gets free SQL, and every handler is a plain def. Other internal roles keep the project-level facts they can already see through the existing routes in the registry (notification log, stage events, file access analytics); the per-person views below are Executive and IT Admin only.

- project_timeline(project_id, since, until, limit, cursor): a time-ordered merge of stage_events, pm_stage_events, audit_log (entity=project plus the project's file, rfq and proposal entity ids), file_access_events (0161, actor_id and actor_role), notifications, email_log, proposal_send_events (0104), estimator rounds (estimator_submissions, 0050), auto_send_runs, bid_outcomes, the abandoned, failed-to-bid and deleted markers, and the project open log that POST /projects/{id}/opens writes to project_open_events (0117), each row carrying actor_kind and the human's name so the label can be derived.
- file_access_report(project_id or user_id, window): view, download and export counts per actor and file from file_access_events, plus page and Project Details open counts per user from project_open_events ("how many times was this opened and by whom"). Prefetch rows and rows with actor_kind='assistant' are excluded by default. With a user_id filter it is sensitive_read: confirm card, human-entered reason, assistant_evidence_access row.
- estimator_timeline(project_id): assignment, the send-to-estimator email_log row (when the estimator received it), the estimator's bell read_at, first file open, plans log views, round opened and sealed times, file-update sends and acknowledgements. It is a per-person view of the external estimator, so it is sensitive_read and logged like user_activity.
- notification_history(project_id or user_id): GET /projects/{id}/notification-log for a project; a new paged per-user route for one person's notices with read_at and dismissed_at, sensitive_read with the reason rule when filtered by user_id.
- user_activity(user_id, window): audit_log, stage_events, file_access_events, project_open_events, proposal_send_events, email_log, todos, estimator_notes, rfp_emails.review_by and llm_call_log (new user_id column) for one person, excluding user.* management actions. sensitive_read: the confirm card names the person and the reason the human types or edits, and the call is logged to assistant_evidence_access with the human-entered reason and the approval id. The same rule covers every evidence call whose filter is a user_id, including ai_usage(user_id), so the "evaluate employees" use has complete oversight, not partial.
- change_history(project_id, field): audit_log project.update patches and the pricing, quote, markup, labor, proposal and file actions, with before-values reconstructed by walking earlier patches and a note when the first value predates the trail.
- ai_usage(user_id or project_id, window): llm_call_log cost and counts.
- build_report(spec): a builder over the same sources plus the analytics GETs; spec is a whitelisted enum plus filters. It returns a Table block for the first 10 rows, totals, and a signed xlsx link produced by a new POST /analytics/assistant-report that reuses the bid-invitations xlsx exporter. Reports containing actual bid amounts are refused for recipients outside ACTUAL_BID_VIEWER_ROLES.

Each tool returns names over ids, a why line, the source row ids so the answer can cite them, and the truncated flags of the underlying caps, so the assistant says "narrow the range" instead of reporting a partial count as the total. The explaining-what-happened skill orders the ladder: timeline first, then the specific report, then raw rows.

### Evidence sources inventory

Scope: every table in the backend that records who did what, when, on a project or for a user. All paths are under bdr_be/. Role shorthand, from app/core/roles.py: WRITER = estimating engineers (both focuses), estimating_admin, executive, it_admin (line 29). INTERNAL = WRITER plus the read-only accountant (line 36). Exec/IT = executive, it_admin. Routes marked [BIDDING] are mounted behind the BIDDING_ENABLED flag and return 404 when it is off (app/main.py:746-779).

All of these tables have RLS forced with no policies. The backend reads them through the sync service-role client, so the router is the only gate. Every handler that calls the SDK must stay a plain def.

| # | Source | Table (migration) | Key columns | Question it answers | Exposed today (route, gate) | Paginated |
|---|---|---|---|---|---|---|
| 1 | Project page opens | project_open_events (0117_project_open_events.sql:18) | project_id, user_id, kind (project or details), opened_at | How many times, and when, each internal user opened a project page or its Project Details box | Write: POST /projects/{id}/opens (app/routers/projects.py:1579, require_internal; the accountant's beacon is a no-op). Read: only aggregated inside GET /analytics/labor-engineer (app/routers/analytics.py:393, INTERNAL) [BIDDING], filtered to labor-focused engineers (app/services/labor_engineer_report.py:192). No raw per-user read route exists | n/a (no raw route) |
| 2 | File access | file_access_events (0161_file_access_events.sql:22) | actor_id, actor_role, project_id, source (7 kinds), file_id, filename and category snapshots, action (view, download, export, prefetch), export_id, detail | Who opened, downloaded or exported which file, and how many times. Includes the external estimator, because downloads are gated by require_project_assignment | GET /analytics/file-access (app/routers/analytics.py:264, INTERNAL) [BIDDING]: a summary per (project, file, user) plus the event feed, with filters for project_id, file_id, actor_id, source and action. GET /projects/{id}/files/{file_id}/access (app/routers/files.py:844, require_internal). Writes come from files.py:804 (download), files.py:831 (opened beacon), bid splitter, submittals, emails, plans reader and PM documents | Yes. limit 100 (max 500) plus offset. The scan is capped at 20,000 rows (app/services/file_access.py:44) and truncated is set when the cap is hit |
| 3 | Stage transitions (bidding) | stage_events (0003_workflow_tables.sql:27, category added in 0078_stage_categories.sql:55) | project_id, category, from_stage, to_stage, actor_id, note, entered_at | When each category lane moved and who moved it. Sent time is the entry into submitted | GET /projects/{id}/stage-events (app/routers/workflow.py:29, INTERNAL) [BIDDING]. Also merged as stage.advance into GET /analytics/activity | No. The route returns the full list |
| 4 | Stage transitions (PM) | pm_stage_events (0057_pm_core.sql:65) | project_id, from_stage, to_stage, actor_id, note, entered_at | When a won job moved through the PM stages | GET /pm/projects/{id}/stage-events (app/routers/pm.py:192, require_pm_read = INTERNAL) | No |
| 5 | Audit trail | audit_log (0006_system_tables.sql:34) | actor_id, action (about 200 distinct actions, for example project.update, quote.select, proposal.send, file.upload, estimator.submit, estimator.log_view, user.update), entity, entity_id, payload, created_at | Who changed what. project.update stores the patch, which holds new values only (app/routers/projects.py:1293) | GET /analytics/activity (app/routers/analytics.py:238, INTERNAL) [BIDDING]: audit_log and stage_events merged, filterable by actor, action and project. The project filter only matches entity='project' rows (app/services/analytics_metrics.py:1409-1411) | Yes. limit 50 (max 200) plus offset. Each source is capped at 1,000 rows per window (analytics_metrics.py:1394) |
| 6 | In-app notifications | notifications (0006_system_tables.sql:3; dismissed_at and rfq_id in 0035, email_log_id in 0091, metadata in 0118) | user_id, project_id, type, message, read_at, dismissed_at, email_log_id, metadata | Who was told what, when, and whether they read or dismissed it. The estimator's assigned bell with its read_at is evidence that the estimator saw the assignment | GET /notifications (app/routers/notifications.py:11): the caller's own rows, undismissed, last 50. All recipients appear in the notification log (row 8) | No (the bell returns the last 50) |
| 7 | Outbound email | email_log (0006_system_tables.sql:14) | to_addrs, subject, body, graph_message_id, status, error, project_id, rfq_id, sent_by, created_at | What BDR emailed, to whom, when, and whether Graph accepted it | Only through the notification log (row 8) | See row 8 |
| 8 | Notification log (assembled) | reads notifications and email_log, classified against rfqs, proposal_sends, auto_send_gcs, file_send_batches and submittal tables (app/services/notification_log.py:574) | grouped entries with recipients and delivery status | Every notice and email the project produced, newest first | GET /projects/{id}/notification-log (app/routers/notification_log.py:28, require_internal, rate limited) | No. Capped at 500 notifications and 500 emails (notification_log.py:49-50), and truncated is set when a cap is hit |
| 9 | Estimator assignment | estimator_assignments (0005_pricing_and_assignments.sql:5; sent_to_estimator_at and returned_at in 0036_estimator_turnaround.sql:9) | estimator_id, assigned_by, created_at, due_at, expires_at, revoked_at, sent_to_estimator_at, returned_at | When the estimator was assigned and first emailed, when they handed back, and whether access was revoked or expired | GET /projects/{id}/assignments (app/routers/estimator.py:702, require_writer) [BIDDING]. Turnaround is aggregated in GET /analytics/estimator (app/routers/analytics.py:173, INTERNAL) | No |
| 10 | Plans and specs sends to estimator | file_send_batches, file_send_recipients, file_send_batch_files (0076_file_send_batches.sql:84, 138, 160) | kind (initial, update, reassign), sent_by, sent_at, summary counts, recipient email, email_log_id, file ids | Exactly which files went to which estimator, when, with which email | GET /projects/{id}/files/send-batches (app/routers/files.py:363, require_project_assignment). Internal users see recipients and sender. The estimator sees only their own batches, and each estimator read is audited as estimator.log_view | No |
| 11 | Estimator rounds | estimator_submissions (0050_estimator_rounds.sql:20), project_files.submission_round | estimator_id, round, submitted_at, summary (per-category counts) | When each round of deliverables came back and what it contained | Internal: GET /projects/{id}/file-freshness (app/routers/change_review.py:34, require_internal) shows the latest round and staleness only. Estimator: their own rounds in the portal detail (estimator.py:1293). No internal route lists the full round history | No |
| 12 | Change review acks | change_review_acks (0050_estimator_rounds.sql:56) | project_id, user_id, last_reviewed_at | Whether a given internal user acknowledged the latest revision round | Only as the caller's own needs_review flag (change_review.py:34). No cross-user read | n/a |
| 13 | Estimator notes and read marks | estimator_notes (0025_estimator_notes.sql:6), estimator_note_reads (0029_note_reads.sql:6), mentioned_user_ids (0160) | author_id, body, created_at; per-user last_read_at | What was said between the team and the estimator, and whether each person read it | GET /projects/{id}/notes and /unread (app/routers/notes.py:166, 230, require_project_assignment). Read marks are exposed only as the caller's unread count | No |
| 14 | Proposal transmissions | proposal_sends (0024_send_out_proposals.sql:57), proposal_send_events (0104_proposal_send_events.sql:23) | gc_id, kind (initial, resend), via (email, external), recipients, status, error, email_log_id, sent_at, sent_by | Which GCs got our number, when, by whom, re-sends and failures | GET /projects/{id}/proposals (app/routers/proposals.py:384, internal check in the handler). Events are attached per GC via send_events_by_proposal (app/services/proposal_send.py:1927) | No |
| 15 | Auto send runs | auto_send_runs (0159_auto_send.sql:58), auto_send_email_log (0159_auto_send.sql:153) | project_id, bid_date, status, trigger, reasons, markup_pct, amounts, sent_at, triggered_by, accepted_at and accepted_by, accept_prior_state | Why auto send did or did not fire, at what markup, and who accepted it | GET /projects/{id}/auto-send (app/routers/auto_send.py:80, require_internal) [BIDDING] via auto_send.project_summary (app/services/auto_send.py:1594) | No |
| 16 | Bid outcome | bid_outcomes, bid_gc_outcomes (0033_bid_outcome.sql:20, 44) | result, winning_gc_id, notes, recorded_by, recorded_at; per GC award, our_amount, winning_amount | Won or lost, against whom, by how much, and who recorded it | GET /projects/{id}/outcome (app/routers/outcome.py:19, require_internal) [BIDDING] | No |
| 17 | Project status | Derived, never stored (app/services/project_status.py), from projects.current_stage, abandoned_at and abandoned_by, failed_to_bid_at, failed_to_bid_by, failed_to_bid_reason and failed_to_bid_note (0162_failed_to_bid.sql:8-22), plus bid_outcomes | status (active, sent, won, lost, no_award, declined, abandoned, failed_to_bid, no_bid) | Why a bid is dead and who killed it | Project detail and list routes in app/routers/projects.py (INTERNAL) | Lists yes, detail n/a |
| 18 | Deleted projects | deleted_projects (0141_project_delete_archive.sql:41) | project_id (no FK), number, name, stage, gc snapshot, reason code, note, deleted_by | Who deleted a project and why | GET /deleted-projects (app/routers/deleted_projects.py:81, require_restore = IT Admin) | Yes |
| 19 | Bid drafts | bid_drafts (0107_bid_drafts.sql:9) | name, number, data, created_by, created_at, updated_at | Who started an intake and left it unfinished | GET /bid-drafts (app/routers/bid_drafts.py:305, require_writer) [BIDDING] | No (fixed limit of 500) |
| 20 | RFP ingestion | rfp_emails (0120_rfp_email_ingestion.sql:16; created_project_id in 0130_rfp_project_creation.sql:139), rfp_email_sightings (0120:79) | from_address, received_at, llm_answer and confidence, review_decision and review_by and review_at, continued_by, status, flag_reason, created_project_id; mailbox per sighting | When an invite arrived, how the classifier ruled, who reviewed it, and which project it created | GET /rfp-emails and /rfp-emails/{id} (app/routers/rfp_emails.py:751, 1254, RFP_VIEW roles, scoped to the viewer's mailboxes per profiles.rfp_mailboxes). Created projects: GET /rfp-created (app/routers/rfp_created.py:506) | Yes (limit plus offset) |
| 21 | PM mailbox emails | ingested_emails (0061_email_ingestion.sql:19) | project_id, direction, message_at, from, to, subject, mailbox | What was emailed about a job in the PM mailbox | GET /emails?project_id= (app/routers/emails.py:76, require_internal). Today this has no mailbox scoping (see the PM mailbox scoping fix) | Yes. limit 50 (max 200) plus offset |
| 22 | To-dos | todos (0026_todos.sql:7) | user_id, title, due_date, is_done, completed_at, created_at, last_nudged_at | What a person committed to and when they finished it | GET /todos?user_id= (app/routers/todos.py:98, require_internal; any internal user can read a teammate's list) | No |
| 23 | Reminder and digest ledgers | due_reminder_log (0032_due_reminders.sql:13), due_digest_log (0116_due_digest.sql:14) | per project, user and day claims | Whether a person was reminded before a due date | None (internal ledgers) | n/a |
| 24 | Calling in | call_in_entries (0142_calling_in.sql:75), call_in_calls (0142:27) | project_id, gc, round, opened_at, call outcome, actor | Whether the GC follow-up calls were made | GET /calling-in, /calling-in/projects/{id} (app/routers/calling_in.py:222, 228, require_internal). Analytics at GET /analytics/calling-in (analytics.py:312) | No |
| 25 | LLM jobs and calls | llm_jobs (0094_llm_job_queue.sql:44), llm_call_log (0094:135) | jobs: job_type, feature, project_id, status, attempts, error_kind, created_by, started_at, finished_at. Calls: feature, provider, model, tier, ok, error_kind, duration_ms | Why an AI step was slow or failed | GET /llm-monitor/summary, /failures, /queue (app/routers/llm_monitor.py:106, 195, 228, require_dev) | failures only |
| 26 | AI spend events | ai_spend_events (0152_plans_reader.sql:294) | kind (cap_hit, cap_reset, cap_changed), amount_usd, actor_id | When the AI Estimating cap was hit or changed, and by whom | GET /ai-spend (app/routers/ai_spend.py:69, require_spend_admin) returns status | No |
| 27 | Browser agent jobs, turns and steps | browser_agent_jobs (0157_browser_agents.sql:204), browser_agent_turns (0157:268), browser_agent_steps (0157:325) | jobs: agent_type, status, outcome, project_id, source_email_id, cost_usd. Steps: seq, kind, actor (worker, playbook, model, bdr, helper, admin, system), actor_user_id, summary, tool_name, redacted args and result, url_before and url_after, screenshot, at | What the browser agent did, step by step, and who intervened. The closest existing model for recording an assistant as an actor | GET /browser-agents/jobs, /jobs/{id}/steps, /jobs/{id}/turns (app/routers/browser_agents.py:206, 224, 235, viewer = estimating_admin, it_admin, executive or dev, per core/roles.py:115). GET /browser-agents/admin/history (line 416, IT Admin). DEV ONLY: migrations 0157 and 0158 are not in prod | Yes. Steps: limit 200 (max 500) plus after_seq. Lists: cursor |

Evidence tools mapped to routes. Each tool calls an existing service function, or one thin new one, inside a plain def handler under the analytics router. The tool runs as the user (bdra_ principal) and is gated on that user's role, never the service role's.

| Tool | Backing data | Route | Role gate | Notes |
|---|---|---|---|---|
| project_timeline | stage_events, pm_stage_events, audit_log (entity=project plus the project's file, rfq and proposal entity ids), file_send_batches, estimator_submissions, proposal_send_events, auto_send_runs, bid_outcomes, project_open_events, the abandoned, failed-to-bid and deleted markers, notification-log entries | NEW GET /analytics/evidence/projects/{id}/timeline. Reuses analytics_metrics.activity_log, notification_log.build, send_events_by_proposal, auto_send.project_summary and the outcome overview | EVIDENCE_ROLES, with actual_bid_at redacted via redact_for_role as on the project routes | One merged, typed event list with cursor pagination and truncated flags passed through from the underlying caps. The activity_log project filter misses non-project entities, so the new service must also match child entity ids |
| file_access_report | project_open_events (page and details opens), file_access_events (file view, download, export) | Files: EXISTING GET /analytics/file-access (analytics.py:264). Page opens: NEW GET /analytics/evidence/projects/{id}/opens, returning counts per user and kind plus first and last open | EVIDENCE_ROLES for the per-person views; the existing file-access route keeps its INTERNAL gate for the UI; sensitive_read with reason when filtered by user_id | Exclude prefetch by default. Exclude rows with actor_kind='assistant' by default (see the actor attribution gap) |
| estimator_timeline | estimator_assignments (created_at, sent_to_estimator_at, due_at, revoked_at, expires_at, returned_at), file_send_batches and recipients joined to email_log status, the estimator's assigned and files_updated notifications with read_at, first file_access_events row where actor_role='estimator', estimator.log_view audit rows, estimator_submissions rounds | NEW GET /analytics/evidence/projects/{id}/estimator-timeline | EVIDENCE_ROLES, sensitive_read with reason (a per-person view of the estimator) | Answers: assigned at, emailed at (Graph accepted or failed), bell read at, first file opened at, plans log viewed at, round 1 returned at. Email read receipts do not exist |
| user_activity | audit_log by actor_id, stage_events by actor_id, file_access_events by actor_id, project_open_events by user_id, proposal_send_events.sent_by, email_log.sent_by, todos, estimator_notes.author_id, rfp_emails.review_by, llm_call_log.user_id | NEW GET /analytics/evidence/users/{id}/activity | EVIDENCE_ROLES, sensitive_read: human-entered reason and approval id written to assistant_evidence_access | Cross-project view of one person, cursor paginated. Excludes user.* management actions per decision 14; the assistant links to the user management page instead. Needs new indexes (see gaps) |
| notification_history | notifications, email_log | Project: EXISTING GET /projects/{id}/notification-log (notification_log.py:28). Per user: NEW GET /analytics/evidence/users/{id}/notifications | Project: INTERNAL (existing). Per user (all their notices, read_at, dismissed_at): EVIDENCE_ROLES with sensitive_read and reason, or the user themself without one | The existing route is capped at 500 plus 500 with no pagination. The new one pages |
| change_history | audit_log project.update patches, pricing.*, quote.select and quote.deselect, markup.set, labor.review, proposal.*, file.upload and file.delete, and stage_events | NEW GET /analytics/evidence/projects/{id}/changes | EVIDENCE_ROLES | Patches hold after-values only. The tool reconstructs before-values by walking earlier patches, and says so when the first value predates the audit trail |
| build_report | Any of the above. spec is a whitelisted enum (project_timeline, file_access, estimator_turnaround, user_activity, notification_history) plus filters, never free SQL | NEW POST /analytics/assistant-report. Builds the workbook with openpyxl, following build_bid_invitations_workbook (app/services/bid_invitations_excel.py:107). The existing export path streams bytes (analytics.py:367); the new route uploads via storage.upload_file (app/services/storage.py:76) to assistant-reports/{user_id}/{uuid}.xlsx and returns storage.signed_url (storage.py:114) with a short TTL | EVIDENCE_ROLES. Rate limited with export_rate_limit | Times in America/Los_Angeles, matching the other reports. Each build is audited. A cleanup sweep removes old report objects |

Gaps: evidence that does not exist yet.

1. No agent marker on any actor column. audit() writes actor_id only (app/services/notifications.py:256). The same is true of stage_events, file_access_events, email_log.sent_by, proposal_send_events.sent_by and notifications. Migration 0171 (dev Supabase only, never BDR_Prod) adds actor_kind and assistant_run_id, and audit() reads the provenance contextvar; see Actor provenance details. browser_agent_steps (actor, actor_user_id, actor_label) is the closest existing pattern.
2. The assistant's own reads would inflate open counts. If the assistant downloads files through the normal routes, those reads land in file_access_events as the user. They are stamped with actor_kind='assistant' and excluded from file_access_report by default. The assistant never calls POST /projects/{id}/opens; that route is not in the registry.
3. No raw read of project opens. project_open_events is only read inside the labor-engineer report, and only for labor-focused engineers. Counts per user and per project need the new route.
4. Project page views are partial. Only internal users are tracked, the accountant is never recorded, and tracking started at 0117 (older opens are blank). The beacon throttles on the client side, so a row means one visit, not one render. Estimator portal page opens are NOT tracked. Only the estimator's file opens (file_access_events) and plans log views (estimator.log_view) are.
5. File access history is limited before 0161. It was backfilled from audit_log file.download, file.preview and file.export rows. Old exports have no per-file list, and pre-0161 downloads may include hover prefetches that were logged as real downloads (noted in the 0161 header).
6. No email read receipts or link clicks. email_log proves that Graph accepted a message, not that anyone opened it. The bell's notifications.read_at is the only "they saw it" signal.
7. No time on page, section views, tab views or session length. Login and session events are not in audit_log. They live only in Supabase Auth logs, which no route exposes.
8. Change history has no before-values. project.update stores only the patch (projects.py:1293). Before-values are recorded only for internal_bid_at (as the bid-date-change email). Audit rows on non-project entities (project_file, rfq, quote) carry no project_id column, so they are missed by the project filter in /analytics/activity.
9. Internal users cannot see full estimator round history. Only the latest round is exposed internally (file-freshness). change_review_acks and estimator_note_reads expose only the caller's own state, so "did Alex acknowledge round 3" needs a new read.
10. Hard deletes cascade evidence. Deleting a project cascades stage_events, project_open_events, notifications, estimator rounds and acks, and proposal_send_events. Only audit_log, file_access_events and the deleted_projects archive survive.
11. llm_call_log has no project_id or user. AI failures can be tied to a project only through llm_jobs.project_id; 0170 adds user_id and assistant_run_id to llm_call_log.
12. Missing indexes for user_activity. There is no actor index on stage_events, no user index on project_open_events, and no index on email_log.sent_by or proposal_send_events.sent_by. 0171 adds them (dev only).
13. Caps. activity_log caps each source at 1,000 rows and the notification log at 500 plus 500. file_access scans up to 20,000 rows. The tools pass truncated through, and the assistant says "narrow the range" instead of reporting a partial count as the total.
14. Visibility decision for executives. RFP queue rows are scoped by profiles.rfp_mailboxes, and executives are currently scoped deliberately (0134). Decision 2 reverses that for PM email and for read_mailbox, not for the RFP queue. Whether Executive and IT Admin assistants also see all RFP rows is listed under Open decisions.
15. PM mailbox scoping. GET /emails (app/routers/emails.py:76) is gated only by require_internal. The scoping fix ships as the phase 0 standalone ticket, long before the assistant gets any email evidence tool, so the assistant cannot read past it.

## UI

AssistantLauncher mounts in components/AppShell.tsx inside the shell root after the main element (AppShell.tsx:109-112), hidden on the estimator portal and until MfaGate passes, gated on features.assistant (fail closed in lib/features.ts next to browser_agents at features.ts:71). It opens a docked end-side panel on .panel-elevated with z between the Sidebar's 45 and Modal's 50, hidden when the AI Estimating ChatDrawer is open. Below the md breakpoint the panel is a full-screen sheet with a back control and stacked cards, and the launcher hides on routes with bottom action bars. The launcher is page-aware: usePathname plus the route shapes send {path, project_id} with each message and the panel says "Talking about 26.10.7267". An AssistantProvider in app/(app)/layout.tsx owns the thread store and polls at CHAT_POLL_MS (2000 ms, lib/aiEstimating.ts:1114) only while a message is pending, backing off when the tab is hidden. ChatDrawer.tsx thread state and MessageBubble are lifted into components/chat/ first so there is no third copy under the strict Next 16 hooks lint. The launcher reads llm_health and shows "unavailable because ..." when the route is down, disabled or over cap; a busy model shows "thinking" and retries inside the turn.

Full page at /assistant with Chat, Routines, Memory, Activity (Revert and Discard links), Mailbox access (Executive and IT Admin) and Settings tabs, kept out of subAppForPath (features.ts:120). Settings > My assistant card and /settings/assistant sub-page: display name (the panel header only; records always render the derived "<User>'s assistant"), application-mail availability with the live own mailbox and configured shared scope listed, write-only credentials per site in the AgentSecretsSection pattern, calendar toggle, permission summary. The users admin page gets an Assistant tab per user for IT Admin: disable, export, purge, and read conversations with a reason.

Assistant text renders escaped, never through RichText, with no auto-links or remote images. Server-issued blocks (table, project link, action card, memory chip) render with the existing Table and Badge components. Attachment previews inside cards reuse the files preview endpoint (Gotenberg for office files) in an un-sandboxed PDF iframe inside a Modal. Cards bind to assistant_approvals ids only and resume after reload. UI strings live under assistant.* in all six locale files with English fallback; the assistant's replies themselves are English only. The sidebar label for /agents becomes "Browser jobs" and lib/help/agents is retitled. Assistant tasks appear on the To-dos page.

## Data model

All tables: uuid PK, RLS enabled and forced with no policies, SELECT and EXECUTE revoked from anon and authenticated, owner FK user_id references profiles(id) on delete cascade where the row is private to the person, actor FKs set null (0012), project_id with no FK or set null (never RESTRICT, 0141), idempotent DDL, ending with notify pgrst. Ledgers that must survive the person (assistant_tool_calls, assistant_approvals, assistant_schedule_runs, assistant_mailbox_access, assistant_evidence_access, assistant_conversation_views) store user_id as a plain uuid plus user_label with no FK. 0170 defines assistant_refuse_change() under its own name and binds only assistant triggers to it, never browser_agent_refuse_change (0157:662), which prod does not have.

Migration ordering. Prod has 0001-0153 and 0159-0162; 0154 drops and re-creates llm_jobs_job_type_check with its own full list (0154:70-71), as 0152 did (0152:32-33). Rule: 0154 to 0158 are applied to prod before 0170, written into the release note and the prod memory note, with the owner's explicit approval at release time; never applied by the assistant build itself, and nothing in this document runs against BDR_Prod. Defensively, 0170 also widens the constraint by reading pg_get_constraintdef, appending the assistant types and re-creating it, so an out-of-order apply cannot drop them. A repo check in scripts/ fails when two migrations both re-list llm_jobs_job_type_check.

0170, assistant core:

- assistant_profiles: user_id PK, display_name null (default "<first word of full_name>'s assistant"), enabled, timezone default 'America/Los_Angeles', daily_cost_cap_usd default 5, quiet_hours, calendar_mode check ('context','all'), legal_hold, timestamps. assistant_settings: id=1 check, enabled, model, global_daily_cap_usd default 50, disabled_tools text[], holidays date[], updated_by (no mailbox_exclusions column: the exclusion list is the MAILBOX_EXCLUSIONS setting, so the phase 0 scoping ticket and read_mailbox read one source).
- assistant_delegations; assistant_conversations (surface check ('panel','page','schedule','task'), mode check ('normal','mailbox_review') default 'normal', tainted_at, mailbox_tainted_at, both set once and never cleared); assistant_messages (role, content, blocks jsonb with reasoning items, tool_calls, status check ('pending','running','waiting_approval','waiting_review','done','failed'), llm_job_id set null, tokens, cost_usd, tainted, mailbox_tainted, used_memory_ids; partial unique index on conversation_id where pending or running); assistant_tool_results (out-of-line payloads with expires_at, purged nightly).
- assistant_tool_calls (append-only): user_id plus user_label, conversation_id, message_id, run_id, seq, idempotency_key, phase check ('begun','completed','failed'), UNIQUE(idempotency_key, phase), lease_owner, tool_name, tool_version, effect, sub_app, method, path_format, args_redacted (8 KB cap), payload_sha256, before_snapshot, after_snapshot, result_summary, ok, error_kind, project_id no FK, approval_id, tainted_context, duration_ms. RPC assistant_tool_call_begin.
- assistant_approvals: user_id plus user_label, conversation_id, message_id, tool_call_id, action_kind, payload, payload_sha256, preview, precondition jsonb, status check ('pending','approved','rejected','expired','executing','executed','failed'), expired_reason, decided_by, decided_at, decided_session_aal, execute_job_id, executed_at, result, email_log_id set null, expires_at, superseded_by (the new row minted on changed_since_preview); partial index (user_id) where pending; trigger assistant_approval_immutable refuses any update to payload, payload_sha256, preview, precondition, user_id or action_kind and any backwards status move.
- assistant_memories (with origin_tainted boolean not null default false) and assistant_memory_events with tsv generated column, GIN tsv and gin_trgm_ops(title), cap triggers; assistant_memory_revisions (pending edits to active rows, approved by the user); assistant_schedules and assistant_schedule_runs; assistant_tasks (kind check ('split_into_project','instruction')); assistant_files (staging: filename, storage_path, size_bytes, sha256, mime, consumed_at, expires_at; caps 450 MB per file, 10 files per message, 2 GB per user; MIME allowlist matching bid_split.check_upload at bid_split.py:349; sha256 dedupe against project_files; 7-day purge of unconsumed rows); assistant_events, assistant_subscriptions, assistant_inbox; assistant_conversation_views (viewer_id, viewer_label, owner_id, conversation_id, reason, created_at, append-only); assistant_eval_lock.
- Alters: llm_jobs job_type widening; llm_call_log add user_id and assistant_run_id; projects add assistant_idempotency_key; bid_split_jobs add assistant_task_id; todos add due_at, project_id, source, assistant_task_id. The price entries for the assistant model and claude-opus-5 are a code edit to services/llm_pricing.py PRICES (Changes required), not a migration.

0171, actor provenance: on every table an assistant tool writes, two columns beside the untouched FK column: actor_kind text not null default 'human' check ('human','assistant','system') and assistant_run_id uuid null with no FK, plus a check that actor_kind <> 'assistant' or assistant_run_id is not null, and a partial index on assistant_run_id. First set: audit_log (0006:34, actor_id), stage_events (0003:27, actor_id), pm_stage_events (0057:65, actor_id), estimator_notes (0025, author_id), quote_notes (0137, author_id), file_access_events (0161, actor_id), email_log (0006:14, sent_by), proposal_send_events (0104, sent_by), labor_reviews (0005:19, reviewed_by), project_files (uploaded_by, following the impersonated_by precedent at 0159:214-222), and the four tables the phase 1 and 2 write tools create rows in: projects (created_by, 0002:63, written by create_project_draft), project_gcs (0002:73, no actor column today; the two new columns are its only provenance), todos (user_id, 0026:11, plus the new source column) and bid_split_jobs (created_by, 0111:50). Without these four the registry test below would fail on day one, and a project the assistant creates would render the human as its creator. notifications gets no columns; the keys ride in its metadata jsonb (0118:48). No actor_label column: the label "<User>'s assistant" is derived at render time from the human's current full_name, so a rename never rewrites history and the string can be translated. The same migration adds the user_activity indexes (stage_events actor_id, project_open_events user_id, email_log sent_by, proposal_send_events sent_by) and the optional ingested_emails (mailbox, message_at desc) index. The PM mailbox scoping fix needs no migration. A contextvar set by the shim lets notifications.audit() (notifications.py:256) and the other writers fill the two columns without touching 60 router modules' signatures. notification_email._TYPE_META, _deep_link, NotificationsBell destination() and PM_NOTIFICATION_TYPES (features.py and features.ts:153) gain the assistant.* types; assistant_conversation_id joins _SELF_SCOPING_METADATA_KEYS (notifications.py:54). Wherever the frontend shows an actor name, actor_kind='assistant' shows the derived label with a small badge; that includes three places that show no actor today and gain one: the project detail header (a "Created by" line), the projects list (badge on rows created by an assistant) and the To-dos page (app/(app)/todos/page.tsx, badge on rows with source='assistant').

### Actor provenance details

Owner decision (2026-10-21): "The record shall show X user's agent as the actor." Records the assistant writes show the actor as "<Full Name>'s assistant". The human stays the accountable principal underneath.

What exists today (verified):

| Table | Actor column | Where it is defined | Main writer |
|---|---|---|---|
| audit_log | actor_id (set null on delete since 0011) | 0006_system_tables.sql:34, 0011_audit_actor_set_null.sql:6-9 | services/notifications.audit() at notifications.py:256, with about 290 call sites |
| stage_events | actor_id | 0003_workflow_tables.sql:27-35 | services/workflow._emit_event (workflow.py:400), routers/projects.py:883, services/rfp_create._seed_state (rfp_create.py:1437-1442), and the SQL functions in 0140_external_submissions.sql:558 and 0159_auto_send.sql:534 |
| pm_stage_events | actor_id | 0057_pm_core.sql:65-73 | services/pm_workflow.append_pm_stage_event (pm_workflow.py:60-67) |
| estimator_notes (project notes) | author_id | 0025_estimator_notes.sql:6-13. Also has source (0080) and mentioned_user_ids (0160) | routers/notes.py:296 |
| quote_notes | author_id | 0137_quote_notes.sql:17 | routers/rfqs.py:1256 (the author is joined at rfqs.py:1162) |
| file_access_events | actor_id, actor_role | 0161_file_access_events.sql:22-43 | services/file_access._row (file_access.py:71), used by both record (115) and record_many (146) |
| email_log | sent_by | 0006_system_tables.sql:14-24 | services/graph_email.send_mail log_row (graph_email.py:485-497) |
| proposal_send_events | sent_by | 0104_proposal_send_events.sql:43 | services/proposal_send.py:1911 |
| labor_reviews | reviewed_by | 0005_pricing_and_assignments.sql:19-26 | the labor route (PUT /projects/{id}/labor, pricing.py:456-470) |
| project_files | uploaded_by, impersonated_by (0159:215) | 0159_auto_send.sql:214-222 | the upload routes and the splitter |
| notifications | none (only the recipient user_id). metadata jsonb was added in 0118_late_gc_pricing_approval.sql:48 | 0006_system_tables.sql:3 | notify_role (notifications.py:57), notify_roles (121), notify_user (173) |
| todos | user_id (the owner, cascade) | 0026_todos.sql:11 | routers/todos.py (insert at todos.py:134) |
| bid_drafts | created_by | 0107_bid_drafts.sql:22 | routers/bid_drafts.py |
| projects | created_by (0002_core_tables.sql:63) | 0002_core_tables.sql:63 | routers/projects.create_project (projects.py:842 sets created_by) |
| project_gcs | none (project_id and gc_id only) | 0002_core_tables.sql:73-79 | projects.py:861 inside create_project and POST /projects/{id}/gcs (projects.py:1653) |
| bid_split_jobs | created_by (set null) | 0111_bid_file_splitter.sql:50 | rfp_file_drop.py:242, bid_splitter.py:263 and rfp_split.py:1019 (created_by=user_id) via the drop routes |

There is no notification_log table. The per-project log is built on read from notifications and email_log; see services/notification_log.py (sender resolved at notification_log.py:531-546).

A grep finds about 166 actor-style uuid references profiles columns across 69 migration files. Tagging all of them would be scope creep. Only the tables an assistant tool actually writes need provenance, and the rule for later phases is: the migration that ships a new write tool also adds the two columns to every table that tool writes. A registry test (below) fails the build otherwise.

Precedent already in the schema: 0159 added impersonated_by to project_files and estimator_submissions (0159_auto_send.sql:214-222). In that design uploaded_by names the estimator and a second column names the real actor behind the write. CurrentUser.impersonated_by (core/deps.py:70) does the same thing at request level. Assistant provenance follows that pattern.

Where actor names render today:

- Backend resolvers: analytics_metrics.py:1448-1462 (the Activity feed row, over audit_log plus stage_events); file_access.py:273, 402 and 440; notification_log.py:531-546; notes.py:42 and 174 (the _AUTHOR_JOIN); rfqs.py:1162; rfp_testing.py:777 (shows "System" when actor_id is null).
- Frontend: components/NoteList.tsx:123; components/QuoteNotesModal.tsx:282; components/NotificationLogModal.tsx:193, through personLabel at 271; app/(app)/analytics/activity/page.tsx:186; app/(app)/analytics/file-access/page.tsx:216, 391 and 452; components/FileAccessModal.tsx:106 and 150.
- The PM stage history (app/(app)/pm/projects/[id]/page.tsx:186-198) shows no actor at all today.

Schema (migration 0171_assistant_provenance.sql, dev project only). On each table above that an assistant tool writes, add two columns and leave every existing FK column alone: actor_kind text not null default 'human', checked to be 'human', 'assistant' or 'system'; assistant_run_id uuid, nullable, with no FK; and a check that actor_kind <> 'assistant' or assistant_run_id is not null. actor_id, author_id, sent_by, reviewed_by and uploaded_by keep holding the human's profile id. Every FK, every "who did this" filter (the Activity actor_id filter at analytics_metrics.py:1405-1406 and 1423-1424) and every per-person report keeps working. Assistant work is attributed to the person who delegated it, and actor_kind splits it out.

```sql
-- 0171_assistant_provenance.sql. Idempotent. Dev project (BDR) only until release.
do $$
declare t text;
begin
  foreach t in array array['audit_log','stage_events','pm_stage_events','estimator_notes',
                           'quote_notes','file_access_events','email_log','proposal_send_events',
                           'labor_reviews','project_files',
                           'projects','project_gcs','todos','bid_split_jobs']
  loop
    execute format('alter table public.%I add column if not exists actor_kind text not null default ''human''', t);
    execute format('alter table public.%I add column if not exists assistant_run_id uuid', t);
    execute format('alter table public.%I drop constraint if exists %I', t, t || '_actor_kind_chk');
    execute format('alter table public.%I add constraint %I check (actor_kind in (''human'',''assistant'',''system'')) not valid', t, t || '_actor_kind_chk');
    execute format('alter table public.%I validate constraint %I', t, t || '_actor_kind_chk');
    execute format('alter table public.%I drop constraint if exists %I', t, t || '_assistant_run_chk');
    execute format('alter table public.%I add constraint %I check (actor_kind <> ''assistant'' or assistant_run_id is not null) not valid', t, t || '_assistant_run_chk');
    execute format('alter table public.%I validate constraint %I', t, t || '_assistant_run_chk');
    execute format('create index if not exists %I on public.%I (assistant_run_id) where assistant_run_id is not null', t || '_assistant_run_idx', t);
  end loop;
end $$;
create index if not exists stage_events_actor_idx on public.stage_events (actor_id, entered_at desc);
create index if not exists project_open_events_user_idx on public.project_open_events (user_id, opened_at desc);
create index if not exists email_log_sent_by_idx on public.email_log (sent_by, created_at desc);
create index if not exists proposal_send_events_sent_by_idx on public.proposal_send_events (sent_by, sent_at desc);
create index if not exists ingested_emails_mailbox_msg_idx on public.ingested_emails (mailbox, message_at desc);
notify pgrst, 'reload schema';
```

Design choices:

- Why a constant default: Postgres 11 and later add a not null default constant column as a metadata-only change, so even audit_log is not rewritten. The not valid plus validate pair keeps the lock short.
- No FK on assistant_run_id: audit rows must outlive any pruning of the assistant ledger; on delete set null would quietly turn an assistant row into a human row, and the check constraint would block that anyway; 0160 made the same call for mentioned_user_ids (0160_note_mentions.sql:6-13).
- No actor_label column: the label is derived when it is rendered, from the human's current full_name plus an i18n string. A stored label goes stale when someone is renamed, and it cannot be translated. The owner approved the fixed name "assistant", so there is nothing per-user to store; assistant_profiles.display_name names the panel, not the record. If assistants become user-named later, the API joins assistant_profiles, still with no column here.
- 'system' and old rows: 'system' is reserved for future writes that have no human behind them. Existing rows are not backfilled. A null actor_id already renders as "System" (rfp_testing.py:777), and that rule stays.
- notifications gets no new columns. The metadata jsonb carries {"actor_kind":"assistant","assistant_run_id":...}. Dismissal filters match per key (notifications.py:228), so the extra keys are harmless.

Backend: one contextvar, set in one place. New module app/core/provenance.py:

```python
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass

@dataclass(frozen=True)
class AssistantActor:
    user_id: str   # the human the assistant acts for
    run_id: str    # assistant run / tool-call ledger id

_actor: contextvars.ContextVar[AssistantActor | None] = contextvars.ContextVar(
    "bdr_assistant_actor", default=None
)

def current() -> AssistantActor | None:
    return _actor.get()

@contextmanager
def acting_as_assistant(user_id: str, run_id: str):
    token = _actor.set(AssistantActor(user_id, run_id))
    try:
        yield
    finally:
        _actor.reset(token)

def fields(actor_id: str | None) -> dict:
    """Columns to merge into an actor-bearing insert. Empty for human writes, so
    code deployed before the migration never sends the new keys (PGRST204)."""
    a = _actor.get()
    if a is None:
        return {}
    if actor_id is not None and actor_id != a.user_id:
        return {}  # the writer named a different human; not the assistant's act
    return {"actor_kind": "assistant", "assistant_run_id": a.run_id}
```

Where it is set: only in the ASGI shim the assistant tool dispatcher uses (The shim is never the served app). The shim wraps the call:

```python
with acting_as_assistant(user_id, run_id):
    await app(scope, receive, send)
```

Approval-executed actions and scheduled runs also go through the shim, so there is no second setter.

Why the shim and not get_current_user: get_current_user is a sync dependency, and FastAPI runs it through run_in_threadpool, which runs it in a copy of the context, so a value set there is gone before the handler runs. A value set in the shim's coroutine, before the app is awaited, is inherited by every later threadpool hop, by asyncio.to_thread (the existing behavior is documented at services/llm_gate.py:31) and by Starlette background tasks, all of which run inside the same await app(...). services/llm_gate.py:69-72 already scopes tiers with a contextvar in the same way. The executor's dedicated per-call event loop (Agent loop) is the caller of the shim, so the context is entered inside that loop and does not leak to the server loop.

Defense in depth in core/deps.get_current_user: the bdra_ branch checks that the token's (user_id, run_id) equals provenance.current(). If a contextvar is present with a non-bdra_ token, or the ids do not match, the request gets a 401. CurrentUser gains via_assistant_run_id: str | None = None for handlers that want it, next to the existing impersonated_by at deps.py:70. The contextvar cannot be set from the network; it exists only in process.

Writers that merge **provenance.fields(actor_id):

1. notifications.audit() (notifications.py:256). One edit covers all of the roughly 290 call sites.
2. workflow._emit_event (workflow.py:400), projects.py:883 and rfp_create._seed_state (rfp_create.py:1439).
3. pm_workflow.append_pm_stage_event (pm_workflow.py:60-67).
4. The estimator_notes insert (notes.py:296) and the quote_notes insert (rfqs.py:1256).
5. file_access._row (file_access.py:71). This one matters most: the owner wants executives to see "how many times the project was opened" as evidence about employees, and untagged assistant reads would inflate a person's open counts.
6. graph_email.send_mail log_row (graph_email.py:485-497). This is also where the "Sent by assistant" marker goes on the outgoing message itself (decision 6).
7. The proposal_send_events insert (proposal_send.py:1911).
8. The labor_reviews upsert (pricing.py:456-470) and the project_files inserts reached by split-drop.
9. notify_role, notify_roles and notify_user merge the same keys into metadata.
10. Backend message builders that put a name in text use a helper, provenance.display_actor(full_name), which returns "<name>'s assistant" inside an assistant context. Examples: project_merge.notify_message (project_merge.py:468-483) and the note-mention notice.
11. The four create paths of phase 1 and 2: create_project (projects.py:842, where created_by is set) and the project_gcs insert beside it (projects.py:861) plus POST /projects/{id}/gcs (projects.py:1653); the todos insert (todos.py:134); and the bid_split_jobs insert reached by the split-drop route (the bid_split_jobs inserts are rfp_file_drop.py:242, bid_splitter.py:263 and rfp_split.py:1019) and by the attach-split-job route, which links an existing job instead of inserting one.

The two SQL functions that insert stage_events themselves are 0140 (Mark submitted) and 0159 (auto-send accept). Both are on the assistant deny-list (Permissions and safety, item 4), so no assistant can reach them. A test pins that. If either route is ever allowed, it gains a p_assistant_run_id parameter.

Read side: add actor_kind and assistant_run_id to these selects: analytics_metrics.py:1400-1424, the file_access read selects (column list at file_access.py:48-50), the email_log select at notification_log.py:72, and notes.py:174. workflow.py:29-40 and pm.py:192-200 already select *. Each resolver adds actor_kind next to actor_name. In file_access.py:398-442, the per-user grouping key becomes (actor_id, actor_kind), so "Tom Moore" and "Tom Moore's assistant" are separate rows. The Activity feed and File Access analytics get an actor_kind filter: human, assistant or all, default all, with counts labeled separately. For the external estimator role, the notes API strips assistant_run_id; estimators see the label, never the run id.

Frontend rendering rule. Add one helper, lib/actor.ts. Every renderer listed above calls it instead of reading full_name directly:

```ts
export type ActorKind = "human" | "assistant" | "system";
export function actorName(name: string | null | undefined, kind: ActorKind | undefined,
                          t: Translate, fallbackKey = "actor.formerUser"): string {
  if (kind === "system") return t("actor.system");
  const who = name || t(fallbackKey);
  return kind === "assistant" ? t("actor.assistantOf", { name: who }) : who;
}
```

- English string: "actor.assistantOf": "{{name}}'s assistant". Add it to all six locales; the English text is acceptable everywhere, matching how the RFP namespace was handled.
- Assistant rows also get a small squared "Assistant" badge from components/ui. Executives, IT Admins and the owner can hover it to see the run time and follow a link to the run in /assistant Activity. Other viewers see only the badge.
- Call sites to switch: NoteList.tsx:123, QuoteNotesModal.tsx:282, NotificationLogModal.tsx:271 (personLabel), analytics/activity/page.tsx:186, analytics/file-access/page.tsx:216, 391 and 452, and FileAccessModal.tsx:106 and 150. New call sites, since these render no actor today: the project detail header under app/(app)/projects/[id] (a "Created by" line from projects.created_by plus actor_kind), the projects list rows, and app/(app)/todos/page.tsx (source='assistant' rows).

Why not a shadow profile per assistant:

| Concern | Shadow profile | Columns on the actor tables (chosen) |
|---|---|---|
| Identity | profiles.id references auth.users(id) (0002_core_tables.sql:15), so every assistant needs a real auth user, which is a login-capable identity. It also needs a fake unique email (0002:17). | No new identities. The bdra_ principal loads the human's own profile, so permissions match exactly. |
| Role | role is one enum column (0002:18). The shadow must copy the human's role and be re-synced on every role change. Any drift means the assistant's permissions differ from the person's. | The human's role is read on every call. |
| Broadcasts | notify_role selects every active profile with a role (notifications.py:57). Shadows would get bell rows and mirrored emails for every broadcast, and would show up in mention pickers, the Users admin list, digests and per-user analytics unless every one of those queries learns to exclude them. | Nothing to exclude. |
| Evaluating employees | Assistant work sits under a different id. Every per-person report needs a join back to the human or it misses that work. | actor_id is the human, so existing per-person filters already include assistant work, and actor_kind separates it. |
| Labels | Free ("Tom's assistant" stored as full_name), but it goes stale on rename and cannot be translated. | Derived at render time. |
| Schema change | None to the actor tables. | Two columns on 14 tables now, added per tool later. |

A shadow profile saves fourteen alter tables but creates new auth identities, role syncing, and an exclusion clause in every query that lists users. Not worth it.

Tests:

- tests/test_provenance.py: fields() is empty outside an assistant context and inside a different human's context, and returns the kind plus run id inside acting_as_assistant; reset restores the outer value; the value survives asyncio.to_thread and Starlette run_in_threadpool; a value set inside a sync dependency does not reach the handler, which documents why the shim is the only setter.
- audit(), _emit_event, append_pm_stage_event, file_access._row and send_mail: each sends the two keys only inside the context, and never sends them for human writes (safe before the migration is applied).
- deps: a contextvar with a non-bdra_ token gives a 401. A mismatched run_id gives a 401.
- Registry test: every table touched by every write tool has both columns in the migration set (14 tables in 0171, including projects, project_gcs, todos and bid_split_jobs for the phase 1 and 2 create tools). The 0140 and 0159 routes are on the deny-list.
- Read side: Activity and File Access return actor_kind, the file-access per-user groups split human from assistant, and the estimator notes response has no assistant_run_id.
- FE: lib/__tests__/actor.test.mjs (same harness as lib/__tests__/browserAgentsView.test.mjs) for actorName, and a check that actor.assistantOf exists in all six locales.

Mail and calendar (phase 5, current application-access design): new migrations at the next free number provide assistant_mailbox_access, durable sends/quotas and calendar dedupe; assistant_evidence_access already exists in 0180. email_log adds approval_id and assistant_send_id if absent. Applied 0182 contains the historical OAuth connection/state milestone and is unused under the current decision. Forward migration 0183 has now been applied to dev, retiring all 0182 RPC grants and its profile trigger; applied history was not rewritten. No tenant changes occurred. Browser phase 6 still requires its own next-free migration for principal and credential/session records.

## Changes required to the existing app

- core/deps.py: bdra_ branch with the delegation check and the provenance contextvar match; CurrentUser.via_assistant_run_id.
- core/provenance.py: the contextvar, acting_as_assistant, fields and display_actor.
- New bdr_be/app/assistant/: shim.py (marker, firewall over path_format, provenance context), principal.py, registry.py, executor.py (dedicated loop per call, taint, recipient binding, snapshots, phase ledger, lease renewal), repo.py (user_id first), skills/, evals/, reports/.
- core/config.py: ASSISTANT_ENABLED, ASSISTANT_SCHEDULER_ENABLED, ASSISTANT_TOKEN_SECRET, ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED, SHARED_MAILBOXES (shared_mailbox_set, used by both the PM Emails scope and the assistant mail tools), MAILBOX_EXCLUSIONS (mailbox_exclusion_set, read by the PM Emails full view and by read_mailbox), USER_CRED_KEK, caps; boot validators; the config.py:727 comment about tenant-wide mail access rewritten once Appendix A has run.
- core/features.py and lib/features.ts: assistant key, computed per user in GET /features.
- services/llm.py: features assistant and assistant_extract; complete_messages with reasoning replay.
- services/llm_queue.py: assistant_turn, assistant_continue (model continuation after a split job) and assistant_approval_execute (no model, one route) specs, own claim pass, max_attempts_override, _FEATURE_BY_TYPE entries (llm_queue.py:143), permanent failure for a missing profile.
- services/llm_gate.py, llm_pricing.py, core/ratelimit.py: user_id on log_call; PRICES rows for the assistant model and claude-opus-5 (llm_pricing.py:98; already present); principal-aware keys in _check and check_outbound_email.
- services/mailbox_visibility.py (new, pure) and routers/emails.py scoping on every list, count and detail route, with the mandatory email.viewed_other_mailbox audit (phase 0 standalone ticket, released ahead of the assistant).
- routers/assistant.py: settings, conversations, messages, approvals (CAS over id, status, user, expiry, payload_sha256 and precondition, then enqueue), schedules, memories and memory revisions, files, POST /assistant/browser-tasks (user principal only), mailbox-access and conversation-view admin routes, under require_internal and the user's own JWT; the Graph-backed tools (email_send, calendar) are open to the Accountant under the same approval card, the browser tools and every BDR write are not.
- routers/projects.py: GET /projects/search above line 1140; GET /projects/due-checks; POST /projects/{id}/split-drop under the bidding mount lifting rfp_file_drop.drop (rfp_file_drop.py:278) from behind the RFP router gate (rfp_files.py:47-55; env RFP_INGESTION_ENABLED, older spelling RFP_INGEST_ENABLED, config.py:429-432 with the alias note at config.py:104), keeping require_writer plus require_project_assignment exactly as the lifted route has them (rfp_files.py:139-151) so an Accountant's assistant cannot file files, with a server-side copy from staging; POST /projects/{id}/attach-split-job; AssistantProjectUpdate schema; assistant_idempotency_key; the merge route moves project_note memories.
- File Updates lock behaviour: before the handoff lock, split-drop proceeds; after the package is sent (files.py handoff_locked and check_upload_lock), the route refuses and the assistant offers send-file-updates as an external_send approval; the continuation reports "filed whole, not split" as a distinct outcome whenever rfp_file_drop's whole-file fallback fired and never reports success for it.
- services/bid_split.py _after_done (bid_split.py:1845): when bid_split_jobs.assistant_task_id is set, enqueue assistant_continue, which confirms filing via rfp_split.resync_after_run (rfp_split.py:2094).
- services/due_digest.py exposes compute_checks to the new route; services/notifications.py audit(), notify_role, notify_roles and notify_user read the contextvar; the writers listed under Actor provenance details merge provenance.fields; notification_email and NotificationsBell gain assistant.* types.
- routers/users.py offboarding step before reset-mfa, deactivation, role change and delete_user (users.py:744); the "RFP mailboxes" editor relabeled "Mailboxes".
- routers/analytics.py evidence routes under /analytics/evidence and POST /analytics/assistant-report (phase 4); routers/plans_reader.py notes search and run-creation routes, with plans_reader_runs._write_output (plans_reader_runs.py:1835) writing plans_reader_notes.
- main.py lifespan: scheduler loop and cancel tuple; main.py never touches the shim. pyproject.toml declares python-dateutil.
- Frontend: launcher, panel, provider, /assistant page, /settings/assistant, components/chat kit, AssistantActionCard, block renderer, lib/actor.ts and the actor badge at the nine call sites, Browser jobs rename, users admin Assistant tab, i18n.
- Tests: every tool as each of the seven roles with each sub-app flag off; bdra_ branch (network refusal through TestClient, is_dev forced false, impersonation ignored, revoked delegation refused, contextvar mismatch refused); firewall against path_format (DELETE and every deny-listed route 403 even for it_admin); phase-ledger resume including a begun-without-completed write that parks; approval CAS double-click across two workers; precondition mismatch; the double-render race (two tabs holding the same card while the row underneath is superseded, exactly one click succeeds and only against the hash it rendered); the normal self/shared allow-set (an Executive's email_read on a non-shared, non-self mailbox is refused before Graph, byte-identical to the unknown case); application-mail identity binding (sender derives only from live profiles.email; normal reads use the live primary email plus admin-maintained mailbox mappings and shared set; stale profile/sender approvals refuse after remap); sticky conversation taint across turns; the Mailbox review tool list; an IT Admin bdra_ principal cannot create an org-principal browser job; scheduler double-worker; recipient binding including body hrefs; assistant PATCH never changes current_stage; rate-limit isolation; three-round store=False loop; feature switch independence from PLANS_READER_ENABLED; mailbox visibility and emails router scoping; provenance; eval harness in CI.
- Docs: this file and a Help Center topic distinct from lib/help/agents.

## Phased rollout and sizing

Phase 0, foundations (3.5 to 4 weeks). The PM mailbox scoping ticket first, as a standalone change with no assistant dependency: SHARED_MAILBOXES and MAILBOX_EXCLUSIONS settings, services/mailbox_visibility.py, emails.py scoping with the mandatory audit, open decision 13 settled inside it, released to staging and prod ahead of everything below under the normal release approval. Then flags and kill switches; 0170; bdra_ principal, shim, firewall; registry with core and bidding read tools; complete_messages with reasoning replay; assistant_turn spec, claim pass and max_attempts_override; phase ledger RPC; launcher, panel, /assistant Chat tab, Settings card; components/chat kit lift; eval harness skeleton; the test matrix. Story 1 ships on dev and staging. The owner receives Appendix A to review, not yet to run.

Phase 1, confirmed writes (2 weeks). Approval flow with CAS, continuation job and preconditions; set_labor_numbers, update_project_fields with the stripped schema, add_project_gc, create_project_draft with idempotency key, todo tools; snapshots and Revert; taint across writes including notes and RFP fields; recipient binding scaffold; 0171 provenance, core/provenance.py, the writer edits and the actor badge; principal-aware rate limiter; resume rules. Stories 2 and 3 ship.

Phase 2, splitter handoff (1 week). Staging with caps and purge, split-drop and attach-split-job routes, assistant_tasks, the _after_done hook, assistant_continue, splitter PRICES entries and the per-turn page ceiling, File Updates lock behaviour. Story 4 ships.

Note, 2026-10-22, phase 2 as built (docs/ASSISTANT_PHASE2_PLAN.md; migration 0174 is phase 2's, 0173 went to a release). Three deviations from this design apply until the owner says otherwise. First, assistant_continue runs no model: it posts a server-built message in the conversation, not a model-written continuation. Second, after the hand-off lock split-drop refuses, and the assistant offers no send-file-updates card in its place. Third, chat attachments are PDF only (bid_split.check_upload).

Phase 3, recipe schedules, memory UI, first release (3 weeks). Schedules and runs, claim RPC, loop, four recipes, confirm card, holidays, only_if_changed, auto-pause, Routines tab; inbox schema; memory tools with server-side provenance and pending revisions, Memory tab, Incognito; offboarding hook on every user-admin path; injection corpus gating CI. Story 5 ships. First production release at roughly 9.5 to 10 weeks, after 0154 to 0158 reach prod, Executive and IT Admin first, then all internal roles.

Phase 4, knowledge, reports and evidence (3 weeks). A new migration number at build time (next free after 0174) for plans_reader_notes and backfill, plans tools as routes, plans_read_pages and run_ai_estimating behind cost cards, run_report and the coded recipes, the evidence routes and the xlsx builder, advance_lane with review forced, pm and payroll aggregate reads, memory_search, find_tools, the 150-prompt tool-selection set.

Phase 5, Outlook (application-access design, owner decision 2026-10-26). Reuse existing backend application credentials, with live BDR identity/delegation gates and finite self/shared normal reads. Send from live profile.email behind an immutable approval and the three markers. Keep reason-approved mailbox-review mode, exclusions, append-only access log and weekly digest. Add calendar reads only if existing application permissions support them, database quotas, vetted web tools and the email skill. No Connect Outlook or new app registration. Preserve existing backend mail reach; the historical Appendix A scope-down is not an activation step.

Phase 6, per-user browser tasks (4 weeks, after 0157 and 0158 reach prod and the Windows worker is proven). A new migration number at build time (next free after 0174), site registry, credentials vault, user identity routes, second service account, owner-scoped visibility and help, offline parking, GET-only user_browser_task and status.

Phase 7, proactive router (2 weeks, after phase 5). Event emitters, sweeper, deterministic router, digests, quiet hours, precedence table, feedback buttons.

Phase 8, model triage (built 2026-11-02 in waves 0 to D per docs/ASSISTANT_PHASE8_PLAN.md, after phase 7; a build effort of days, not weeks, because it reuses the phase 7 inbox, router and delivery). Migration 0196 (parts A and B applied on dev, 2026-11-02 and 2026-11-03; not on prod); the triage loop and pure rules module with the gpt-6-luna route and company spend identity; the fourth router-owned kind and its escalation path, the company switch and fault fuse, the per-person digest email opt-out, the system stamp on router emails; the inbox_list read tool; Inbox and Alerts UI, English only; offboarding, retention, evals and a live test on e2e accounts capped at USD 1. Stage 2 (tools and cards for triage) is not built.

## Retention and offboarding

Proposed defaults (owner to confirm):

- assistant_tool_calls, assistant_approvals, assistant_schedule_runs, assistant_mailbox_access, assistant_evidence_access, assistant_conversation_views: 7 years, append-only, never purged by the nightly step.
- assistant_messages and conversations: 2 years, then purged with an IT Admin export offered 30 days before.
- assistant_tool_results (out-of-line payloads): 30 days; 7 days for cross-mailbox and payroll-aggregate results.
- assistant_memories: until archived plus 1 year; assistant_memory_events follow the memory.
- llm_call_log: unchanged at 90 days (config.py:365); per-user daily spend is summed within the day so the window is sufficient.
- assistant_files staging: 7 days unconsumed.
- Browser traces and screenshots (phase 6): 90 days, encrypted.
- assistant_profiles.legal_hold suspends every purge for that user.

Export and IT Admin reads. Export of a user's conversations excludes assistant_tool_results and any mailbox_tainted message unless the recorded reason is legal; both export and read require a reason, write assistant_conversation_views, and the owner sees "Viewed by IT Admin on <date>" at the top of that conversation (open decision whether to also bell the owner). Executives cannot read other users' conversations. The weekly mailbox-access digest goes to Executives and IT Admin alike, so that IT Admin reads of other mailboxes have a reviewer outside IT Admin.

Offboarding runs one audited step from reset-mfa, deactivation, role change and hard delete: revoke the delegation, end schedules, cancel queued assistant jobs, expire pending approvals with reason owner_offboarded, invalidate assistant authority and retire any legacy Microsoft connection, purge credentials and cookie jars, cascade memories and conversations on hard delete, and keep the no-FK ledgers with user_label intact. A deactivated or demoted owner sees no launcher, open conversations become read-only for IT Admin, and pending cards show "expired: account changed".

## Open decisions remaining

1. Per-access notice to the mailbox owner on Executive or IT Admin reads, or only the log and the weekly IT Admin digest.
2. The exclusion list itself (MAILBOX_EXCLUSIONS), which defaults to empty. The owner decided that Executive and IT Admin assistants read everyone's mail, so no mailbox, not even the owner's own, is excluded unless the owner names it here; HR, legal and bookkeeping addresses are candidates only. Whatever the owner names must match the BDR-NO-EXEC-READ tags in Exchange, and the owner initials the final list in the decisions log.
3. Whether the "Sent by assistant" footer should also carry an AI disclosure line on mail to GCs and vendors.
4. Cross-user task assignment and whose permissions apply (recommended: owner's, assigner notified).
5. The retention defaults above, and whether to bell the conversation owner on IT Admin reads.
6. Whether Procore, BuildingConnected and SmartBid terms of service allow automated logins on personal accounts; phase 6 depends on it.
7. Which sites are in the browser site registry at launch.
8. The calendar default: BDR-context events only, or every event N minutes ahead.
9. Pilot group and the success-metric thresholds under the eval harness.
10. Whether a recipe that duplicates the 6:03 digest is refused outright before phase 7 rather than suppressed on digest days.
11. Whether plans note reads open to every internal role when prod moves AI_ESTIMATING_STATE to live, while raw page reads stay restricted.
12. The G3 holiday list for schedules.
13. The PM ingest mailbox: after the scoping fix, mail polled from the owner's personal mailbox is visible only to the owner, Executives and IT Admins. Either point EMAIL_INGEST_MAILBOX and SUBMITTAL_SENDER at a shared mailbox such as office@ (and tag it in Appendix A) or build multi-mailbox polling. Settled inside the phase 0 scoping ticket (recommended: the shared mailbox); it does not block the security fix.
14. Whether the Accountant sees own plus shared mailboxes in PM Emails (recommended) or shared only.
15. The exact address of the admin assistant shared mailbox (adminassistant@g3electrical.com is assumed throughout).
16. Whether Executive and IT Admin assistants also see every RFP queue row, or keep the 0134 scoping there.
17. Superseded 2026-10-26: no separate Reader app is required. A future independently scoped Reader identity or metadata-only review would require a new owner decision.
18. Whether the PM Emails page gives Executive and IT Admin a full view of every ingested mailbox minus the exclusion list (proposed, with the mandatory audit row), or keeps them scoped like everyone else as 0134 does for the RFP queue. The assistant read-all decision implies the former but did not state it.
19. Whether the Accountant's assistant may send email to other users and external contacts under the usual approval card (proposed, per decision 16), or is restricted to notify_self.

## Appendices

### Appendix A: historical Exchange plan, superseded 2026-10-26

Do not execute the commands below as the current Phase 5 rollout. They describe the earlier three-app/delegated design. The owner now requires existing backend application access only. In particular, the old small backend mailbox scopes would break the assistant's own-mailbox reads/sends; the new delegated and Reader registrations are not required. This appendix is retained solely as dated architectural history, not current tenant instructions. Future scope changes require a replacement plan covering all existing backend flows and the new assistant mailbox reach.

#### Historical text: Exchange RBAC for Applications, step by step

- Audience: the G3 Microsoft 365 admin (the owner), who holds Entra and Exchange admin rights personally (decision 3).
- When to run it: after the assistant build ships, in a maintenance window during phase 5. Allow about 3 hours, most of it spent waiting for the cache to refresh. This is tenant admin work, not a BDR_Prod migration, but it changes what production mail flows can do, so it follows the same approval rule as any production change.
- Sources checked: Microsoft Learn, 2025 and 2026 revisions (listed at the end).

#### A.0 What changes, in one table

| Principal | Today | After this appendix |
|---|---|---|
| BDR backend app (existing app registration, client credentials, MS_CLIENT_ID) | Entra application permissions Mail.ReadWrite, Mail.Send and Files.ReadWrite.All, all tenant-wide | Entra keeps Files.ReadWrite.All only. Exchange RBAC grants Application Mail.ReadWrite on scope BDR-App-Mail and Application Mail.Send on scope BDR-App-Send |
| BDR Assistant Reader (new app registration, client credentials) | does not exist | No Entra mail permissions. Exchange RBAC grants Application Mail.Read (or Mail.ReadBasic, open decision 17) on scope BDR-Reader, which covers every user and shared mailbox except the ones tagged for exclusion |
| BDR Assistant (new app registration, delegated, auth code flow with PKCE) | does not exist | Delegated openid profile offline_access User.Read Mail.Read Mail.Read.Shared Mail.Send Calendars.Read, with admin consent for all users. Shared mailbox reach comes from each user's Full Access permission |

Two rules drive the order of the steps below:

1. Exchange RBAC and Entra grants add together. Microsoft's wording: "the assigned permissions are a union operation on the permissions from Microsoft Entra ID and the permissions assigned in Exchange Online RBAC." While the tenant-wide Entra Mail.* grants exist, scoping has no effect. You add the scoped grants first, wait for the cache, and only then remove the Entra grants.
2. Exchange RBAC for Applications applies only to app-only (client credentials) access. It does not limit the delegated assistant. Delegated access is limited by the signed-in user's own mailbox permissions.

#### A.1 Prerequisites

```powershell
# Run in PowerShell 7 on an admin workstation
Install-Module ExchangeOnlineManagement -Scope CurrentUser   # v3.x
Install-Module Microsoft.Graph -Scope CurrentUser

Connect-ExchangeOnline -UserPrincipalName <ADMIN_UPN>
Connect-MgGraph -Scopes "Application.ReadWrite.All","AppRoleAssignment.ReadWrite.All","DelegatedPermissionGrant.ReadWrite.All" -TenantId <TENANT_ID>
```

You must be in the Organization Management role group in Exchange (Global Admin or Exchange Administrator in Entra covers this).

Placeholders used below:

| Placeholder | Meaning |
|---|---|
| <TENANT_ID> | Entra tenant (directory) ID |
| <BDR_APP_ID> | Application (client) ID of the existing BDR backend app (MS_CLIENT_ID in Railway) |
| <PM_INGEST_SMTP> | value of EMAIL_INGEST_MAILBOX in prod (open decision 13 may move it to a shared mailbox) |
| <RFP_INBOX_1>, <RFP_INBOX_2>, ... | each address in RFP_EMAIL_INGESTION_INBOXES_ALLOWED in prod |
| <SUBMITTAL_SENDER_SMTP> | value of SUBMITTAL_SENDER if set. If it is empty, submittals are sent from <PM_INGEST_SMTP> |
| <MS_DRIVE_OWNER_UPN> | value of MS_DRIVE_OWNER (OneDrive only, needs no mail scope) |
| <ADMINASSISTANT_SMTP> | the admin assistant shared mailbox. Confirm the exact address before you start (open decision 15) |
| <EXCLUDED_SMTP_1>, <EXCLUDED_SMTP_2>, ... | the executive read-all exclusion list, if the owner names one (open decision 2; the default is empty), which must match MAILBOX_EXCLUSIONS in Railway |
| <INTERNAL_USERS_GROUP> | a mail-enabled security group holding every internal BDR user |
| <BACKEND_HOST> | the Railway backend host that serves the Connect Outlook callback |

#### A.2 Step 1: Take an inventory and a snapshot

1a. List every mailbox the backend touches. The code (bdr_be/app/core/config.py, services/graph_email.py, graph_inbox.py, rfq_inbox.py, rfp_email_ingest.py) shows these app-only mail paths:

| Mailbox (env var) | Calls | Exchange role needed |
|---|---|---|
| bids@g3electrical.com (MS_SENDER, PROPOSAL_CC, RFQ_CC) | read the RFQ reply inbox, create drafts, createUploadSession for attachments, createReplyAll, /send, /sendMail | Mail.ReadWrite and Mail.Send |
| <PM_INGEST_SMTP> (EMAIL_INGEST_MAILBOX) | poll the Inbox and Sent Items. Also the default sender for submittal requests when SUBMITTAL_SENDER is empty (config.py:1038-1046) | Mail.ReadWrite and Mail.Send |
| <SUBMITTAL_SENDER_SMTP> (only if set) | submittal drafts and sends | Mail.ReadWrite and Mail.Send |
| RFP intake inboxes (RFP_EMAIL_INGESTION_INBOXES_ALLOWED) | read messages and attachments | Mail.ReadWrite |
| office@, <ADMINASSISTANT_SMTP> | nothing today. The assistant reads them through the delegated app, not this one. Tag them only if open decision 13 points PM ingestion at one of them | none unless polled |
| <MS_DRIVE_OWNER_UPN> | /drive/... only, for RFQ bulk-send links and office previews | none (covered by Files.ReadWrite.All, which stays) |
| Dev and staging only: RFP_TESTING_MAILBOX (only when RFP_TESTING_ENABLED; config.py:1031-1032, read by rfp_email_ingest._test_mailbox at rfp_email_ingest.py:551) | test pollers | Tag these only if dev and staging share this same app registration |

Before you continue, confirm the real prod values in Railway. Check every one of these variables: MS_SENDER, EMAIL_INGEST_MAILBOX, SUBMITTAL_SENDER, RFP_EMAIL_INGESTION_INBOXES_ALLOWED, RFP_EMAIL_INGESTION_SHARED_MAILBOXES, MS_DRIVE_OWNER.

1b. Snapshot the current Entra grants. You need this file for rollback.

```powershell
$clientSp = Get-MgServicePrincipal -Filter "appId eq '<BDR_APP_ID>'"
$graphSp  = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"

Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $clientSp.Id |
  Select-Object Id, ResourceDisplayName, AppRoleId,
    @{n='Permission';e={ $r=$_.AppRoleId; ($graphSp.AppRoles | Where-Object Id -eq $r).Value }} |
  Tee-Object -Variable before | Format-Table
$before | ConvertTo-Json | Out-File .\bdr-app-grants-before.json
```

1c. Check for leftover policies and unused custom attributes.

```powershell
Get-ApplicationAccessPolicy          # expected: none (config.py:727 says none is configured)
Get-ManagementScope | Format-Table Name, RecipientFilter
# The tags below use CustomAttribute10, 11 and 12. Confirm nothing else uses them:
Get-Recipient -ResultSize Unlimited -RecipientTypeDetails UserMailbox,SharedMailbox |
  Where-Object { $_.CustomAttribute10 -or $_.CustomAttribute11 -or $_.CustomAttribute12 } |
  Format-Table Name, CustomAttribute10, CustomAttribute11, CustomAttribute12
```

If any of the three attributes is already in use, choose different numbers (1 to 15) and change every filter below to match. In a hybrid setup where mailboxes sync from on-premises AD, set extensionAttributeN in AD instead of using Set-Mailbox.

#### A.3 Step 2: Tag the mailboxes

Tags keep the scopes easy to maintain. Adding a new RFP intake inbox later takes one Set-Mailbox command, with no change to the scope.

```powershell
# Read/write scope for the backend
$mailScope = @('bids@g3electrical.com','<PM_INGEST_SMTP>','<RFP_INBOX_1>','<RFP_INBOX_2>')   # add every RFP intake inbox, and <SUBMITTAL_SENDER_SMTP> if set
$mailScope | ForEach-Object { Set-Mailbox -Identity $_ -CustomAttribute10 'BDR-APP-MAIL' }

# Send scope for the backend (keep it as small as possible)
$sendScope = @('bids@g3electrical.com','<PM_INGEST_SMTP>')                 # add <SUBMITTAL_SENDER_SMTP> if set
$sendScope | ForEach-Object { Set-Mailbox -Identity $_ -CustomAttribute12 'BDR-APP-SEND' }

# Exclusion list for executive read-all (mirror of MAILBOX_EXCLUSIONS in Railway).
# Skip this block entirely when the owner leaves the list empty (open decision 2).
@('<EXCLUDED_SMTP_1>','<EXCLUDED_SMTP_2>') |
  ForEach-Object { Set-Mailbox -Identity $_ -CustomAttribute11 'BDR-NO-EXEC-READ' }
```

Security note: anyone with Exchange admin rights can edit these attributes, so the attributes effectively define the scopes. Write that down in the IT runbook.

#### A.4 Step 3: Create the management scopes and preview them

```powershell
New-ManagementScope -Name 'BDR-App-Mail' -RecipientRestrictionFilter "CustomAttribute10 -eq 'BDR-APP-MAIL'"
New-ManagementScope -Name 'BDR-App-Send' -RecipientRestrictionFilter "CustomAttribute12 -eq 'BDR-APP-SEND'"

# Reader scope: every user and shared mailbox EXCEPT the tagged exclusions
New-ManagementScope -Name 'BDR-Reader' -RecipientRestrictionFilter "((RecipientTypeDetails -eq 'UserMailbox') -or (RecipientTypeDetails -eq 'SharedMailbox')) -and (-not (CustomAttribute11 -eq 'BDR-NO-EXEC-READ'))"
```

The exclusion uses -not (... -eq ...) rather than -ne, so that mailboxes with no value in the attribute are clearly included.

Preview each scope before using it. Run the same filter through Get-Recipient and check the list by eye:

```powershell
foreach ($s in 'BDR-App-Mail','BDR-App-Send','BDR-Reader') {
  $f = (Get-ManagementScope $s).RecipientFilter
  "== $s =="; Get-Recipient -ResultSize Unlimited -Filter $f | Sort-Object Name | Format-Table Name, PrimarySmtpAddress, RecipientTypeDetails
}
# BDR-Reader must NOT list any address on the exclusion list, and must list everything when the list is empty.
```

Alternative for the exclusion list: use a mail-enabled security group with -not (MemberOfGroup -eq '<GROUP_DN>'). You must give the group's distinguished name ((Get-Group '<name>').DistinguishedName). Nested members are not honored, and Entra-only security groups do not work. Group owners could also change membership without Exchange admin rights, which is why the custom attribute is the primary method here.

#### A.5 Step 4: Point Exchange at the service principals

Exchange needs a pointer object for each app. Use the IDs from Enterprise applications, not App registrations. The App registrations page shows the application object ID, which is a different value.

```powershell
$bdrSp = Get-MgServicePrincipal -Filter "appId eq '<BDR_APP_ID>'"
New-ServicePrincipal -AppId $bdrSp.AppId -ObjectId $bdrSp.Id -DisplayName 'BDR Backend (app-only)'
```

For the Reader, create a separate app registration ("BDR Assistant Reader"). It gets its own client credentials (a certificate is preferred to a secret) and no Entra mail permissions. Keeping it separate means a leak of the main backend secret cannot read every mailbox.

```powershell
$readerApp = New-MgApplication -DisplayName 'BDR Assistant Reader' -SignInAudience AzureADMyOrg
$readerSp  = New-MgServicePrincipal -AppId $readerApp.AppId
New-ServicePrincipal -AppId $readerSp.AppId -ObjectId $readerSp.Id -DisplayName 'BDR Assistant Reader (app-only)'
# Add a certificate or client secret to $readerApp in the portal (Certificates & secrets),
# then store it in Railway as the Reader app's credential, never in a BDR table.
```

#### A.6 Step 5: Create the role assignments

```powershell
# BDR backend: read/write on the tagged mailboxes, send only from the send-tagged mailboxes
New-ManagementRoleAssignment -Name 'BDR-Backend-MailRW' -App $bdrSp.Id -Role 'Application Mail.ReadWrite' -CustomResourceScope 'BDR-App-Mail'
New-ManagementRoleAssignment -Name 'BDR-Backend-Send'   -App $bdrSp.Id -Role 'Application Mail.Send'      -CustomResourceScope 'BDR-App-Send'

# Reader: read only. Use 'Application Mail.ReadBasic' instead if the owner decides
# the evidence use needs only metadata (no body, preview body or attachments).
New-ManagementRoleAssignment -Name 'BDR-Reader-MailRead' -App $readerSp.Id -Role 'Application Mail.Read' -CustomResourceScope 'BDR-Reader'
```

Role notes:

- Application Mail.ReadWrite does not include send. The RFQ, proposal and submittal flows create a draft, add attachments and then call /send. Each mailbox they send from needs both roles, so it must be in both scopes. The combined role Application Mail Full Access (Mail.ReadWrite plus Mail.Send) also works, but using two roles keeps sending limited to the smaller send scope.
- Exchange checks the mailbox in the URL path (/users/{mailbox}/...), not the recipients. The send scope therefore only needs the mailboxes you send from.

#### A.7 Step 6: Verify before removing anything

```powershell
# Each line should show InScope = True for in-scope mailboxes
Test-ServicePrincipalAuthorization -Identity $bdrSp.Id -Resource 'bids@g3electrical.com' | Format-Table RoleName, GrantedPermissions, AllowedResourceScope, InScope
Test-ServicePrincipalAuthorization -Identity $bdrSp.Id -Resource '<PM_INGEST_SMTP>'      | Format-Table RoleName, GrantedPermissions, AllowedResourceScope, InScope
Test-ServicePrincipalAuthorization -Identity $bdrSp.Id -Resource '<RFP_INBOX_1>'         | Format-Table RoleName, GrantedPermissions, AllowedResourceScope, InScope
# Negative check: an ordinary user mailbox should show InScope = False for both roles
Test-ServicePrincipalAuthorization -Identity $bdrSp.Id -Resource '<SOME_ORDINARY_USER_SMTP>' | Format-Table RoleName, InScope

# Reader app: in scope for staff, out of scope for the exclusions
Test-ServicePrincipalAuthorization -Identity $readerSp.Id -Resource '<SOME_ORDINARY_USER_SMTP>' | Format-Table RoleName, InScope   # True
Test-ServicePrincipalAuthorization -Identity $readerSp.Id -Resource '<EXCLUDED_SMTP_1>'         | Format-Table RoleName, InScope   # False (skip when the list is empty)

# List all app role assignments (use Format-List * if a column comes back blank)
Get-ManagementRoleAssignment | Where-Object RoleAssigneeType -eq 'ServicePrincipal' |
  Format-List Name, Role, RoleAssigneeName, RoleAssigneeType, CustomResourceScope
```

Test-ServicePrincipalAuthorization bypasses the cache and ignores Entra grants, so a good result here does not mean live calls behave the same way yet.

Now wait at least 2 hours. Microsoft states that changes are cached for "30 minutes and 2 hours depending on the app's recent usage". The BDR backend polls every 120 seconds, so treat it as an active app: 2 hours.

#### A.8 Step 7: Remove the unscoped Entra Mail.* grants (keep Files.ReadWrite.All)

```powershell
$mailRoleIds = $graphSp.AppRoles | Where-Object { $_.Value -in 'Mail.ReadWrite','Mail.Send','Mail.Read','Mail.ReadBasic','Mail.ReadBasic.All' } | Select-Object -ExpandProperty Id
Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $clientSp.Id |
  Where-Object { $_.ResourceId -eq $graphSp.Id -and $_.AppRoleId -in $mailRoleIds } |
  ForEach-Object { Remove-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $clientSp.Id -AppRoleAssignmentId $_.Id }

# Confirm that only Files.ReadWrite.All (plus any non-mail roles) remains
Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $clientSp.Id |
  Select-Object @{n='Permission';e={ $r=$_.AppRoleId; ($graphSp.AppRoles | Where-Object Id -eq $r).Value }}
```

Then, in Entra admin center > App registrations > BDR app > API permissions, remove Mail.ReadWrite and Mail.Send from the configured list (use the "..." menu, then Remove permission). If you skip this, anyone who later clicks "Grant admin consent" puts the tenant-wide grants back without warning.

Force a new token. App-only access tokens last about 60 to 90 minutes, and the old token still carries the Mail.* roles. Restart the backend service in Railway (prod, plus staging if it shares the app) so MSAL fetches a new token.

Optional: decode a new token (for example on jwt.ms) and confirm that roles contains only Files.ReadWrite.All.

#### A.9 Step 8: Smoke tests (live, after the restart)

Live Graph check with a fresh app-only token, run from the admin workstation:

```powershell
$secret = Read-Host 'BDR client secret' -AsSecureString
$plain  = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret))
$tok = (Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token" -Body @{
  client_id='<BDR_APP_ID>'; client_secret=$plain; scope='https://graph.microsoft.com/.default'; grant_type='client_credentials' }).access_token
$h = @{ Authorization = "Bearer $tok" }

Invoke-RestMethod -Headers $h -Uri "https://graph.microsoft.com/v1.0/users/bids@g3electrical.com/messages?`$top=1&`$select=subject"        # expect 200
Invoke-RestMethod -Headers $h -Uri "https://graph.microsoft.com/v1.0/users/<SOME_ORDINARY_USER_SMTP>/messages?`$top=1&`$select=subject"  # expect 403 ErrorAccessDenied
Remove-Variable plain, secret, tok
```

BDR functional checks. Run every one of these. A 403 in the backend logs means a mailbox is missing from a scope.

| # | Test | Proves |
|---|---|---|
| 1 | RFQ bulk send on a test project with a drawing set larger than rfq_drawings_inline_limit_mb | OneDrive upload and createLink on MS_DRIVE_OWNER (Files), draft and createUploadSession on bids@ (ReadWrite), /send from bids@ (Send), CC copy landing in bids@ |
| 2 | RFQ reply poller picks up a vendor reply in bids@ | ReadWrite on bids@ |
| 3 | Proposal send to a test GC address, with the CC to bids@ | Send on bids@ |
| 4 | RFP ingestion: send a test invite to each intake inbox | ReadWrite on each RFP inbox |
| 5 | PM ingest poller assigns a new email | ReadWrite on <PM_INGEST_SMTP> |
| 6 | Submittal request send | Send on <PM_INGEST_SMTP> or <SUBMITTAL_SENDER_SMTP> |
| 7 | Office preview of an .xlsx | Files on MS_DRIVE_OWNER (unchanged) |
| 8 | read_mailbox from an Executive assistant: a staff mailbox (works, after the confirm card) and an excluded mailbox (403, and BDR's own refusal before the call) | BDR-Reader scope and MAILBOX_EXCLUSIONS |

#### A.10 The delegated BDR Assistant app

1. Register the app. In Entra admin center > App registrations > New registration:
   - Name: BDR Assistant. Single tenant.
   - Platform: Web. Redirect URI: the Connect Outlook callback on https://<BACKEND_HOST>, registered byte for byte as BDR's config value, the way rfp_bc_redirect_url is (config.py:959); plus a staging URI if needed.
   - Add a client certificate or secret. A confidential web client with PKCE is the right fit for a FastAPI backend that stores refresh tokens. MSAL Python's initiate_auth_code_flow adds PKCE automatically.
   - Under API permissions > Microsoft Graph > Delegated, add: openid, profile, offline_access, User.Read, Mail.Read, Mail.Read.Shared, Mail.Send, Calendars.Read.

2. Grant admin consent with the portal button ("Grant admin consent for G3 Electrical"), or with PowerShell:

```powershell
$asstSp = New-MgServicePrincipal -AppId '<ASSISTANT_APP_ID>'      # skip if it already exists
New-MgOauth2PermissionGrant -BodyParameter @{
  clientId    = $asstSp.Id
  consentType = 'AllPrincipals'
  resourceId  = $graphSp.Id
  scope       = 'openid profile offline_access User.Read Mail.Read Mail.Read.Shared Mail.Send Calendars.Read'
}
```

3. Limit who can sign in. In Enterprise applications > BDR Assistant > Properties, set Assignment required = Yes, then assign <INTERNAL_USERS_GROUP>. External estimators are not in that group (decision 9).

4. Give users Full Access to the shared mailboxes so that Mail.Read.Shared works. Graph returns shared or delegated mail only when the mailbox owner "has shared his Inbox ... [or] delegated his mailbox". Full Access is the mailbox-wide form of that.

```powershell
# Check what already exists (the owner says all internal users already have access)
'bids@g3electrical.com','office@g3electrical.com','<ADMINASSISTANT_SMTP>' | ForEach-Object {
  Get-MailboxPermission -Identity $_ | Where-Object { -not $_.IsInherited -and $_.User -notlike 'NT AUTHORITY\SELF' } |
    Format-Table Identity, User, AccessRights }

# Grant to the group (one ACE; mailboxes allow at most 500 ACEs)
'bids@g3electrical.com','office@g3electrical.com','<ADMINASSISTANT_SMTP>' | ForEach-Object {
  Add-MailboxPermission -Identity $_ -User '<INTERNAL_USERS_GROUP>' -AccessRights FullAccess -InheritanceType All -AutoMapping $false }
```

- The group must be mail-enabled. Non-mail-enabled security groups "are selectable, but they don't work".
- Full Access also exposes private calendar items in that mailbox.
- Mail.Read.Shared reaches every mailbox the user holds Full Access to, not just these three. Audit existing Full Access grants (for example an exec with delegate access to the owner's mailbox) before launch. BDR refuses any mailbox outside the connected UPN plus SHARED_MAILBOXES before the Graph call, for every role including Executive and IT Admin, but the audit is the hard limit.
- Mail.Read.Shared does not support Graph change notifications, so poll or use delta queries instead.
- To send as a shared mailbox (not planned), the app would need Mail.Send.Shared plus Send As or Send on Behalf rights. The agreed design sends from the user's own mailbox (decision 6).

#### A.11 Sending as the user with a visible "Sent by assistant" marker

The assistant calls POST /me/sendMail with the user's delegated token. Recipients see the user as the sender. Use all three markers below, because each has a different limit:

```json
{
  "message": {
    "subject": "...",
    "body": { "contentType": "HTML", "content": "...<p style=\"color:#666;font-size:12px\">Sent by [User Name]'s assistant in BDR, approved by [User Name] on [date]</p>" },
    "toRecipients": [ { "emailAddress": { "address": "someone@example.com" } } ],
    "categories": [ "Sent by assistant" ],
    "internetMessageHeaders": [
      { "name": "x-bdr-assistant", "value": "<SEND_ID>" }
    ]
  },
  "saveToSentItems": true
}
```

| Marker | Who sees it | Limits |
|---|---|---|
| Signature line in the body | Every recipient, in every client | The only marker everyone sees. Recipients can delete or alter it when they reply or forward. The assistant always appends it on the server side, never through the model's text |
| internetMessageHeaders (x- prefix) | Hidden in normal view. Visible in "message source" and to BDR, which reads it with $select=internetMessageHeaders | Header names must start with x- (X-BDR-Assistant qualifies; header names are case-insensitive). Headers can only be set when the message is created and cannot be changed after sending. Graph has been reported to reject more than 5 custom headers per message (forum and vendor reports, not on a Microsoft Learn page, so test it); BDR uses one. External gateways may strip custom headers. Best used as the machine-readable link back to the BDR ledger row; the value is one opaque send id, never a user, run or approval id |
| categories | The user, on their own Sent Items copy | A category is a label on the item in that one mailbox. Do not expect it to reach recipients. The category color comes from the user's master category list, and creating that list entry needs MailboxSettings.ReadWrite, which is not in the consented scopes. Without it, the label still applies but has no color. The user can create the color once by hand, or you can add MailboxSettings.ReadWrite later |

The record of truth stays in BDR: email_log carries sent_by = the human, actor_kind='assistant', assistant_run_id and approval_id, and renders as "<User>'s assistant".

#### A.12 Verification commands, collected

```powershell
Get-ServicePrincipal | Format-Table DisplayName, AppId, ObjectId
Get-ManagementScope 'BDR-App-Mail','BDR-App-Send','BDR-Reader' | Format-Table Name, RecipientFilter
Get-ManagementRoleAssignment | Where-Object RoleAssigneeType -eq 'ServicePrincipal' | Format-List Name, Role, RoleAssigneeName, CustomResourceScope
Test-ServicePrincipalAuthorization -Identity '<SP_OBJECT_ID>' -Resource '<MAILBOX_SMTP>' | Format-Table RoleName, GrantedPermissions, AllowedResourceScope, InScope
Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $clientSp.Id      # Entra side: Files.ReadWrite.All only
Get-MgOauth2PermissionGrant -Filter "clientId eq '$($asstSp.Id)'" | Format-List ConsentType, Scope
```

Rerun the Test-ServicePrincipalAuthorization block from Step 6 any time someone adds a mailbox tag.

#### A.13 Rollback plan

| Situation | Action | Time to take effect |
|---|---|---|
| A BDR flow returns 403 after Step 7 | Fast path: put the Entra grant back and restart the backend. Leave the RBAC objects in place (they do no harm under the union) so you can keep debugging | Minutes, once the new token is issued |
| A mailbox is simply missing from a scope | Set-Mailbox <smtp> -CustomAttribute10 'BDR-APP-MAIL' (or 12 for send) | 30 minutes to 2 hours (cache) |
| Abandon RBAC entirely | Remove the assignments, then the scopes, then the SP pointers (commands below) | Immediate in config, cache up to 2 hours |

```powershell
# Fast path: re-grant tenant-wide Mail.ReadWrite and Mail.Send (from the Step 1b snapshot)
foreach ($p in 'Mail.ReadWrite','Mail.Send') {
  $role = $graphSp.AppRoles | Where-Object { $_.Value -eq $p -and $_.AllowedMemberTypes -contains 'Application' }
  New-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $clientSp.Id -PrincipalId $clientSp.Id -ResourceId $graphSp.Id -AppRoleId $role.Id
}
# then restart the backend in Railway

# Full teardown of the RBAC objects
Remove-ManagementRoleAssignment 'BDR-Backend-MailRW'  -Confirm:$false
Remove-ManagementRoleAssignment 'BDR-Backend-Send'    -Confirm:$false
Remove-ManagementRoleAssignment 'BDR-Reader-MailRead' -Confirm:$false
Remove-ManagementScope 'BDR-App-Mail' -Confirm:$false
Remove-ManagementScope 'BDR-App-Send' -Confirm:$false
Remove-ManagementScope 'BDR-Reader'   -Confirm:$false
Remove-ServicePrincipal -Identity $bdrSp.Id    -Confirm:$false
Remove-ServicePrincipal -Identity $readerSp.Id -Confirm:$false
```

To roll back the delegated app, run Remove-MgOauth2PermissionGrant -OAuth2PermissionGrantId <id> or set "Enabled for users to sign in" to No on the Enterprise app. To remove Full Access, run Remove-MailboxPermission -Identity <mbx> -User <group> -AccessRights FullAccess -InheritanceType All.

#### A.14 Gotchas

1. ObjectId versus AppId. New-ServicePrincipal -ObjectId takes the service principal object ID (Enterprise applications page, or Get-MgServicePrincipal ... | Select Id). The App registrations page shows the application object ID, which is different and does not work. -AppId is the client ID, which is the same on both pages.
2. Propagation delay. Changes to grants, scopes and tags take 30 minutes to 2 hours to reach live calls. The test cmdlet bypasses this cache. Never remove Entra grants until the 2-hour wait has passed.
3. Union with Entra. If the Entra Mail.* grant remains, scoping does nothing. Also remove it from the configured permissions list, or a future consent click brings it back.
4. Mail.Send versus Mail.ReadWrite. ReadWrite cannot send, and Send cannot create drafts or upload attachments. Draft-then-send flows (RFQ, submittal, createReplyAll) need both roles on the sending mailbox.
5. RFQ bulk-send smoke test is mandatory. It is the one flow that uses all three permission types: Files on MS_DRIVE_OWNER plus ReadWrite and Send on bids@. Files.ReadWrite.All must stay, because Exchange RBAC does not cover OneDrive. Note that it is still tenant-wide over all OneDrive and SharePoint content. Narrowing it with Sites.Selected is a separate project.
6. Submittals send from the PM ingest mailbox when SUBMITTAL_SENDER is empty, so that mailbox needs both tags.
7. Exclusive scopes do not restrict apps. Do not pass -Exclusive and expect it to block an app.
8. Shared app registration across environments. If dev or staging use the same client ID, their test mailboxes need tags too, or those pollers start returning 403.
9. Stale code comment. bdr_be/app/core/config.py:727 (PM ingest section) says the Mail.ReadWrite grant is tenant-wide and "any mailbox works". After this change, a new EMAIL_INGEST_MAILBOX value must be tagged first. Update that comment and the IT runbook.
10. Delegated access is not scoped by any of this. The assistant reaches whatever the user can reach, which is why BDR's delegated allow-set (connected UPN plus the shared mailboxes, never unfiltered for any role) is the only gate on that path. Executive read-all goes through the separate app-only Reader app, and BDR itself limits that tool to Executive and IT Admin roles, to a confirm card with a human-typed reason, and to the exclusion list.
11. Autodiscover does not work with RBAC application roles. This only matters if something later uses EWS.

#### Sources

- Role Based Access Control for Applications in Exchange Online (ms.date 2025-11-25, updated 2026-08): https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac
- New-ManagementScope: https://learn.microsoft.com/en-us/powershell/module/exchangepowershell/new-managementscope
- Filterable properties for the RecipientFilter parameter: https://learn.microsoft.com/en-us/powershell/exchange/recipientfilter-properties
- Add-MailboxPermission (updated 2026-08): https://learn.microsoft.com/en-us/powershell/module/exchangepowershell/add-mailboxpermission
- Grant or revoke API permissions programmatically (ms.date 2025-04-25): https://learn.microsoft.com/en-us/graph/permissions-grant-via-msgraph
- user: sendMail (updated 2026-06): https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0
- message resource type (custom headers rules, updated 2026-04): https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0
- Outlook messages in shared or delegated folders (Mail.Read.Shared): https://learn.microsoft.com/en-us/graph/outlook-share-messages-folders
- Create Outlook category (MailboxSettings.ReadWrite): https://learn.microsoft.com/en-us/graph/api/outlookuser-post-mastercategories?view=graph-rest-1.0
- Five-custom-header limit (community report, unconfirmed by Microsoft docs): https://learn.microsoft.com/en-us/answers/questions/762243/is-there-a-way-to-bypass-the-limit-of-internetmess

Not confirmed from Microsoft Learn: the five-header limit (a 2022 Microsoft Q&A thread and a vendor doc); that categories never reach recipients (written cautiously above); the exact output property name CustomResourceScope on Get-ManagementRoleAssignment (use Format-List * if the column is blank); the 60 to 90 minute app-only token lifetime (general Entra behavior).

### Appendix B: ToolSpec effect tiers at a glance

read: automatic. sensitive_read: confirm card naming the mailbox or person, human-typed reason, logged with the approval id, no write. write_internal: approval, 1-hour expiry, precondition bound, immutable row. external_send: approval, 4-hour expiry, recipients and body hrefs bound, never standing. expensive: cost or page preview then approval. forbidden: deny-list, refused in the shim regardless of role.


## Historical mail architecture, superseded 2026-10-26

The following records the previous decision for audit/history only. It is not an implementation requirement. Current Email and calendar sections above take precedence. Applied migration 0182 belongs to that earlier OAuth milestone; do not rewrite applied migration history. Retire unused tables/RPCs/routes/secrets in a forward change.

### Mail scope, chosen and justified

Three Entra app registrations after the build:

1. The existing BDR backend app (the bids@ app, MS_CLIENT_ID in Railway), scoped down with Exchange RBAC for Applications (Appendix A). Its Mail.ReadWrite reach becomes the tagged scope BDR-App-Mail: bids@, the PM ingest mailbox (EMAIL_INGEST_MAILBOX, config.py:730), SUBMITTAL_SENDER when set, and every RFP intake inbox (RFP_EMAIL_INGESTION_INBOXES_ALLOWED, config.py:757). Its Mail.Send reach becomes the smaller scope BDR-App-Send: bids@ and the PM ingest mailbox, because submittal requests send from the ingest mailbox whenever SUBMITTAL_SENDER is empty (config.py:1038-1046), plus SUBMITTAL_SENDER when set. Its Files.ReadWrite.All grant stays, because RFQ bulk-send needs OneDrive through MS_DRIVE_OWNER (config.py:529) and Exchange RBAC does not cover OneDrive; only the unscoped Mail.ReadWrite and Mail.Send application grants are removed once the RBAC roles are in place, and acceptance includes an RFQ bulk-send smoke test. The config comment at config.py:727 ("any mailbox works") becomes false after this change and is updated with the ticket.
2. BDR Assistant (delegated). Scopes: openid, profile, offline_access, User.Read, Mail.Read, Mail.Read.Shared, Mail.Send, Calendars.Read, admin-consented for all users, with Assignment required = Yes and an internal-users group assigned. Each user connects once through Connect Outlook (an authorization-code flow with the CSRF state ledger copied from bc_client.py:207-253, new_state, state_well_formed and consume_state, and a callback route on the rfp_bc.py:331 pattern whose URI is registered byte for byte as rfp_bc_redirect_url is at config.py:959; PKCE is new here, supplied by MSAL initiate_auth_code_flow, since the BuildingConnected client has none; refresh tokens encrypted with encrypt_with_aad at app/services/browser_agents/secrets.py:132, never 0136's plain-text columns). At the callback BDR reads /me and refuses the connection unless userPrincipalName, case-folded, equals profiles.email or is one of profiles.rfp_mailboxes; the match basis (email or rfp_mailbox, plus the matched address) is stored in assistant_ms_connections.match_basis, re-checked on every token refresh and before every send, and an IT Admin change to profiles.email or rfp_mailboxes revokes the connection with reason profile_remapped. A flow signed with another Microsoft account (a colleague's SSO session on a shared PC, a shared mailbox's own login, an old delegate account) therefore never becomes "self". With this token the assistant reads the user's own mailbox and, through Mail.Read.Shared, the three shared mailboxes, which Exchange allows only because the user already holds Full Access to them; BDR probes each at connect time and records which are reachable. Microsoft, not Python, enforces the boundary. Note that Mail.Read.Shared reaches every mailbox the user holds Full Access to, not only the three; the Reading section refuses anything outside self plus the shared list before the Graph call, and Appendix A audits existing Full Access grants before launch.
3. BDR Assistant Reader (app-only, Mail.Read only, no Entra mail grant), scoped with Exchange RBAC to the management scope BDR-Reader: every user and shared mailbox except those tagged BDR-NO-EXEC-READ. Used only by read_mailbox for Executive and IT Admin assistants. Chosen over granting those users Exchange FullAccess everywhere because FullAccess would also open every mailbox in Outlook, broader than asked and invisible to BDR's log. App-only keeps the reach inside BDR, reason-gated and logged. It is a separate app registration from the backend app so a leak of the backend secret cannot read every mailbox. Mail.ReadBasic is the fallback role if the owner decides the evidence use needs metadata only (open decision).

### Reading

Phase 5 tools over the delegated token: email_search(mailbox, query, window), email_read(mailbox, id) through graph_inbox.get_message with body_type text (graph_inbox.py:103) and capped attachments (graph_inbox.py:173), email_attachment_to_staging (graph_inbox.py:272). mailbox is the connected UPN (assistant_ms_connections.upn, the verified self) or one of the three shared addresses from SHARED_MAILBOXES, for every role including Executive and IT Admin. The allow-set is computed by a dedicated function, assistant.mail_scope.delegated_allow_set(connection, settings) = {upn} | shared_mailbox_set, which never returns None and is deliberately separate from services/mailbox_visibility, whose rule applies to ingested_emails rows only and would have left these two roles unfiltered. Anything else is refused before the Graph call, with a refusal that is byte-identical for an excluded, an unknown and a colleague's mailbox; cross-mailbox reads go only through read_mailbox and the Reader app. A test calls email_read as an Executive with any non-shared, non-self address and asserts the refusal before any Graph call, byte for byte the same as the unknown-mailbox case. Reads are live; ids, subjects and summaries stay in assistant_messages.blocks, while full bodies and attachment text are stored out of line in assistant_tool_results with a 30-day TTL (7 days for cross-mailbox results). The v1 claim that nothing is persisted is dropped; retention and export rules replace it.

read_mailbox(target_upn, reason), phase 5, Executive and IT Admin only, sensitive_read tier: it never runs automatically. The call parks a confirm card naming the target mailbox and the model's proposed reason; the human types or edits the reason (at least 10 characters) and clicks, and the log stores the human-entered reason plus the approval id, never the model's words as the human's justification. It refuses when target_upn is in the exclusion list (settings.mailbox_exclusion_set, env MAILBOX_EXCLUSIONS, default empty, owner-confirm item under open decision 2; the owner edits it in Railway, and the owner is also the one who edits the Exchange tags, so one person holds both lists) or when the conversation is not in Mailbox review mode. Caps: at most 3 distinct target_upn values per turn and 10 per reader per day, and the first read of each mailbox in a week bells the owner. The same exclusion list is tagged in Exchange (CustomAttribute11 = BDR-NO-EXEC-READ, Appendix A step 2) so the Reader app cannot reach an excluded mailbox even if BDR's check is bypassed. The two lists must match: a weekly probe by the Reader app requests one message from each excluded address and expects 403, and the IT Admin page shows "Exchange tag missing" for any address that returns 200. Every call appends to assistant_mailbox_access (reader_user_id, reader_label, target_upn, message_ids, reason, approval_id, conversation_id, created_at) under the assistant_refuse_change trigger. IT Admin sees the log at /settings/assistant/mailbox-access, with the exclusion list shown read-only beside it, and the weekly digest goes to every Executive as well as IT Admin, so IT Admin reads have a reviewer; a change to MAILBOX_EXCLUSIONS is detected at boot by comparing the effective list with the last audit row and is written as audit mailbox_exclusions.changed with a bell to the owner. Per-access notice to the mailbox owner is an open decision. The conversation is read-only and memory-blocked as described under taint.

### Sending

Sends go from the user's own mailbox through delegated Mail.Send (POST /me/sendMail) with saveToSentItems=true, so Outlook holds the record and recipients see the user as the sender. Internal recipients (other users, decision 16) and external recipients are both allowed under the binding rule below. The "Sent by assistant" marker is three things at once, because each has a different reach (Appendix A section A.11): a footer line below the user's signature, "Sent by <User>'s assistant in BDR, approved by <User> on <date>", appended server-side and never by the model, the only marker every recipient sees; one internet message header X-BDR-Assistant: <send_id>, a single opaque uuid minted per send and mapped to run, approval and user in email_log, so no internal identifier reaches a GC or vendor, set through internetMessageHeaders on the sendMail payload (names must start with x-, headers are set only at creation, Graph is reported to reject more than five custom headers per message, and external gateways may strip them, so this is the machine-readable link back to the ledger, not the user-facing marker); and the Outlook category "Sent by assistant" set through categories, which labels only the user's own Sent Items copy (uncolored unless the owner adds it to the master category list, which would need MailboxSettings.ReadWrite and is not requested). Attachments go inline under 3 MB total; larger files go as short-TTL signed download links, as graph_email.py already recommends, so Mail.ReadWrite is not needed. Every send writes email_log with sent_by = the human, actor_kind='assistant', assistant_run_id, approval_id and assistant_send_id, and the test-bench redirect applies. send_mail re-checks assistant_ms_connections.match_basis against the live profile before every call, so a send never goes out under a connection whose Microsoft account no longer matches the BDR profile.

email_draft renders a G3-style preview (email_branding.render_branded_html) with recipients bound as described under taint; the card shows To, Cc, subject, body, every href in the body next to its anchor text, attachment previews, the three marker elements and a "Reply-To is you" line. email_send(approval_id) executes only through the continuation job. Internal recipients show their role on the card. External recipients (GC and vendor contacts) are allowed by contact id or typed address, every send approved; whether the footer should also disclose AI involvement to external parties is an open decision.

### Calendar

Phase 5, delegated Calendars.Read: a 10-minute calendarView poll over a 3-hour horizon with Prefer: outlook.timezone="Pacific Standard Time". Per-user toggle: BDR-context events only (default: the GC on the invite has a bid due this week) or every event N minutes ahead. Calendar writes are not in scope.

