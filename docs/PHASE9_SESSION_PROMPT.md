How to use this file: paste all of it into a new Claude Code session with ultracode on, or just say "read bdr_be/docs/PHASE9_SESSION_PROMPT.md and do it".

# BDR per-user assistant: finish Phase 8, then design and build Phase 9

## Mission

You are picking up the BDR per-user assistant work for G3 Electrical. The owner runs a construction estimating business and is not a developer, so every question you bring to them must be in plain words with a clear recommendation. Phase 8 (model triage of bell-sourced Inbox rows) is built and live-tested on the dev database but not committed, and one defect (P8L-4) is still open. Your job, in order: close Phase 8 and commit it locally with the owner's go-ahead (Part 1); design Phase 9 with the same method used for Phases 7 and 8 and stop for the owner's answers (Part 2); build Phase 9 on dev only (Part 3); and write a prod release checklist that you do NOT execute without the owner's explicit approval at that moment (Part 4). Browser v2 / Phase 6 belongs to another session (bdr-94) and is out of scope, as is any unrelated feature.

## Read first

Read these before doing anything. All paths are absolute.

1. `/Users/thomasmoore/Desktop/BDR/.claude/CLAUDE.md` (project rules)
2. `/Users/thomasmoore/.claude/projects/-Users-thomasmoore-Desktop-BDR/memory/MEMORY.md`, then the notes it links under "Read first", especially `bdr-per-user-assistant-plan.md`, `never-touch-prod-without-approval.md`, `subagents-use-opus-5.md`, `executive-delegation-principle.md`, `bdr-uncommitted-tree-checkout-hazard.md`, `bdr-supabase-migration-ops.md`, `bdr-e2e-api-testing.md`, `bdr-live-llm-test-mac-sleep.md`, `bash-tool-inline-loop-commands.md`, `bdr-event-loop-blocking.md`
3. `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/ASSISTANT_BUILD_LOG.md`: the "How to resume" recipe at the top (lines 3 to 27) and the Phase 8 entries (line 1351, and lines 1374 to the end, about 1456)
4. `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/ASSISTANT_PHASE8_PLAN.md` (the hard rules block at lines 9 to 25, scope 1.2 and 1.2a at lines 43 to 64, section 16 tests and live test at lines 752 to 817, section 17 rollout and file ownership at lines 818 to 891, section 19 owner questions at line 913 and 19.1 follow-ups at lines 946 to 960)
5. `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/PHASE8_OWNER_DECISIONS.md` (source of truth for Phase 8; it overrides the plan where they differ)
6. `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/PHASE8_OWNER_QUESTIONS_BRIEF.md` (the model for the Phase 9 brief; structure at lines 9, 416, 1197)
7. `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/PHASE7_OWNER_DECISIONS.md` (an alternative decisions-file format)
8. `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/ASSISTANT.md` (design doc; decisions log; "Open decisions remaining" at lines 667 to 687)
9. `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/phase8_wi1_manifest.json` (read-only file sha256 manifest, 130 entries)
10. `/Users/thomasmoore/Desktop/BDR/bdr_be/supabase/migrations/0196_assistant_phase8.sql`
11. `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/ASSISTANT_BROWSER_V2_PLAN.md` and `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/BROWSER_V2_RELEASE_RUNBOOK.md` (only to know what bdr-94 holds; do not work on them)

Line numbers below were correct on 2026-11-03 but drift. Re-find them by searching before you cite them.

## Current state (2026-11-03)

Verify every item below yourself before relying on it. Where a fact is marked "verify", the earlier session was not sure.

- Git, backend `bdr_be`: branch `build`, head `72cd333 Add the per-user assistant, phases 0 to 7`. Frontend `bdr_fe`: branch `build`, head `766dab4 Add the per-user assistant UI, phases 0 to 7`. Neither is pushed. Neither is on prod. Both trees are dirty (about 269 backend and 41 frontend paths in `git status --short`).
- Phase 8 is built and live-tested on dev (Supabase project `bpidntbyvoooqvaispup`), and none of it is committed.
- Migration 0196 on dev: part A is in the ledger as three rows (`20261102214014 _a1`, `20261102214057 _a2`, `20261102214155 _a3`); part B as `20261103002332 0196_assistant_phase8_b` (build log line 1408). Verify that B6 (`assistant_triage_set_enabled`) exists on dev; it was moved into part B before B was applied (build log line 1391), and on disk it sits after the PART B marker (`0196_assistant_phase8.sql` line 910, B6 at about 1117), so it is probably there.
- Prod (`zxqkqotlcgbalwoqeeca`) has no assistant migration at all. Per memory it is at 0181 with the non-assistant files in that range live (0173, 0175, 0177, 0178, 0179, 0181); the assistant files (0170, 0171, 0172, 0174, 0176, 0180, 0182 to 0196) and the dev-only files 0154 to 0158 and 0163 to 0169 are absent. Do not read the prod ledger to check this; it is only needed for Part 4, with approval.
- Migrations on disk end at 0196. 0190 and 0191 are committed. 0192 (browser v2, applied on dev), 0193 (Excel edit), 0194 (browser v2 draft, not applied, refuses to run while TODO values remain) and 0196 are untracked. 0195 is reserved by bdr-94 and not on disk. 0197 is free on disk; check the dev ledger too.
- Phase 8 results recorded so far: live eval gate PASS, 41/44 agreement (93.2 percent), 0 escalations on covered negatives, 0 urgent on not-urgent cases, 0 invalid answers, 0.00591 USD; offline injection set 28/28; Phase 8 test files 810 pass, assistant sweep 6,710 pass, frontend 449 pass with eslint and tsc clean.
- SQL checks 108/108 (plan 16.2) and concurrency check 6/6 (plan 16.3) are recorded in memory only. The build log acceptance note (line 1435) says they are "not evidenced in this log". Plan acceptance list 16.6 item 2 is unrecorded.
- Full suite: 7 failures, none Phase 8. 5 known: `test_auto_send` resend gate (uncommitted proposal work), two browser-agent signature tests (browser v2), `tests/test_secreview_route_auth_coverage.py` (addendum route), `tests/test_secreview_rpc_revoke_migration.py` (RPC revoke for 55 older functions). Plus 2 load-dependent flakes whose test ids were never written down; find and name them.
- Defects: P8L-2, P8L-3 and P8L-5 fixed and retested (build log lines 1428 to 1456). P8L-1 and P8L-5 were plan text corrections and are already written into plan 16.5 steps 3 and 10. P8L-4 is open (below).
- P8L-4: the cut-off triage call `llm_call_log 6bd16e16` at 2026-11-03 04:47Z (ok false, `invalid_output`) was logged with 0 tokens and 0 cost, and its spend hold settled at 0. The OpenAI Responses API bills incomplete responses, so it may have been billed. Mitigation shipped: `assistant_triage_model._floor_cut_off_usage` writes `max_out` tokens and a priced cost on the triage run row, plus a warning log line. Not fixed: `llm._openai_usage` and the `llm_call_log` row are unchanged, and both the company cap and the test cap sum `llm_call_log`, so they can under-count a recurrence. The retest forced a truncation and the provider did report usage (1022 in, 16 out, 0.00011 USD), so the zero was not reproduced.
- "Everything urgent": in the live test step 4 (run 78e5e7f5) the real model escalated all three seeded bells as urgent; expected was none, suggest and escalate (build log line 1411). This matches eval miss e05 (urgent where normal or high was expected, line 1397). Other eval misses: s02, c13, c16, c08, t04. Urgent goes to Teams at any hour with no fact check (plan risk 3, about line 896). `assistant_triage_reasoning_effort` defaults to empty because "low" gave invalid answers on the golden set.
- Stale docs to fix: `docs/ASSISTANT.md` line 53 (decision 24) and line 648 (rollout) still say part B is not applied; `docs/ASSISTANT_PHASE8_PLAN.md` line 3 says "Nothing built, nothing applied" and the migration hard rule at line 11 still reads as if 0196 were not yet written or applied; plan line 62 cites open decision 4 at `ASSISTANT.md:669` but it is at 672; the MEMORY.md assistant line still says "Phase 8 BUILT on dev 11-02".
- Stage 2 design text: removed from the Phase 8 plan by owner answer 1 (a). It survives in plain words in `PHASE8_OWNER_QUESTIONS_BRIEF.md` lines 317 to 319, 406 to 446 and 743 to 756, and in full in a session transcript: `/Users/thomasmoore/.claude/projects/-Users-thomasmoore-Desktop-BDR/caf31268-9668-482b-ac0b-db988699a8e2/subagents/workflows/wf_faa2d9fc-ca1/agent-acff29ee82bac8e75.jsonl` (present on 2026-11-03). That transcript is scratch and may age out; if it still exists, copy the draft's sections 5.10 (Stage 2 prep turn), 14.4 (Stage 2 cost) and work item WI-12, plus the Stage 2 parts of 4.3 and 6.2, into a scratch note early (plan 1.2 names 5.10, 14.4 and WI-12 as the removed text). The plan file is untracked, so git has no history of it.

## Hard rules

Verbatim from `/Users/thomasmoore/Desktop/BDR/.claude/CLAUDE.md`:

> when running migrations, migrate to the BDR database not BDR_Prod. BDR_Prod is the production database. You are only allowed to migrate to that database when given explicit permission.
>
> Do not use em dashes anywhere especially on the frontend or on files, unless necessary for technical reasons.

From memory and earlier phases:

1. Databases: dev BDR `bpidntbyvoooqvaispup` only. BDR_Prod `zxqkqotlcgbalwoqeeca` and BDR_Staging `qpfuugcwsdfpxvdgwvwx` are never touched (no SQL, no migration, no env change) without the owner's explicit approval given in this session at that moment. The owner's words: "DO NOT TOUCH THE PRODUCTION DATABASE!!!". Scripts and helpers must refuse any non-dev Supabase URL.
2. No em dashes in code, SQL comments, docs, locale files or frontend text. Guards that look for them write `\u2014`. Scan every changed file for em and en dashes.
3. Git: never `git checkout`, `git restore`, `git stash` or `git reset` on tracked files. The trees hold weeks of uncommitted work from several sessions. Never `git add -A`, `git add .`, `git commit -a` or `git add` on a file that mixes Phase 8 and other hunks. Never modify, stash or reset anything bdr-94 owns. Commit only when the owner says so; push only when the owner says so.
4. Commit method (used 2026-11-02, build log line 1312): index-only staging from a classified file plan (`git hash-object -w` plus `git update-index --cacheinfo`) for files that mix Phase 8 and other hunks, so the working tree is never touched; verify the staged content byte for byte before committing. Keep the git author as it is (the D1c67 noreply email; Vercel blocks other authors). Use the commit attribution trailer the harness gives you in that session. Keep `docs/AI_ESTIMATING_BUILD_SPEC.md` out of every commit (it holds real dollar figures).
5. Secrets: never print `.env` values, keys, tokens or `llm.resolve()` Route objects. Never type a password or one-time code into chat or a terminal. If anything prints a secret, stop and tell the owner it needs rotating.
6. Never touch ports 5051, 5061 or 4500, and never edit `.env`. Pass test flags on the command line only.
7. Never use the owner's real account as a test fixture (the Phase 8 concurrency script did once; it must not happen again). Use the e2e accounts `bdr.e2e.electrical@g3test.dev` and `bdr.e2e.admin@g3test.dev`, password grant for tokens, scramble the password afterwards.
8. Event loop: never call the sync Supabase SDK inside `async def`. Handlers that touch it are plain `def` or use `run_in_threadpool`.
9. Assistant isolation (Phase 8 plan hard rules, lines 5 to 25): only `app/assistant/repo.py` (plus `principal.py` and `evals/run_evals.py`) queries `assistant_*` tables, `user_id` first; nothing under `app/assistant/` names `get_supabase`; `main.py` never contains `app.assistant`; router modules and the triage orchestrator call no model; triage sends nothing itself; no free text to the model. New tables: RLS enabled and forced, no policies, privileges revoked from anon and authenticated. New functions: `security invoker`, `set search_path = pg_catalog, public`, service_role only. Pacific time for windows, UTC in storage. The `tests/conftest.py` `_no_real_supabase` guard must stay green.
10. Supabase connector: apply migrations by hand with `apply_migration`, unique `000N_` prefixes, pieces under about 40 KB, `add column if not exists`. After DDL run `notify pgrst, 'reload schema';` and verify through REST, not only SQL. Destructive text (a top-level drop, or a function body containing delete text) can return "Invalid or expired requestState" and then needs the owner at the screen; subagents cannot get past it. Keep destructive text in a separate part B.
11. Shell: never name a zsh loop variable `path` or `fpath`. Put multi-step loops in a script file and run `bash file.sh`. macOS grep has no `-P`. `echo =====` fails in this zsh.
12. Never claim AI Estimating job types (`plans_reader_*`, `ai_pricing`, `bid_split`) from a test backend.
13. Never set `ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD` on a server.

## Working method

- You are the orchestrator. Keep scoping, judgment and synthesis yourself; delegate execution through ultracode workflows and subagents.
- Model choice: Opus 5.5 for architects, critics, verifiers, core logic (repo, spend, service loop, router delivery, SQL) and anything security related. Sonnet 5.5 for readers and easy builds. Haiku for grunt chores (lint, grep checks, listings, manifest diffs, count pins, dash scans, locale key parity). No Fable unless the owner asks.
- Run long workflows under `caffeinate -i -s`; Mac sleep killed a plan workflow once.
- Phases 7 and 8 method, which you repeat for Phase 9:
  1. Design workflow: Sonnet readers map the tree (Phase 8 used five maps: router, runtime and money, tools, notifications, decisions), one Opus architect writes the plan, Opus critics attack it (security and injection, cost and concurrency, code fidelity), one Opus verifier per finding, an Opus apply pass writes confirmed findings in and logs them in the plan's review log.
  2. The plan follows the Phase 8 plan's section shape: status and hard rules block; 1 scope, non-scope, dependencies; 2 decisions; 3 architecture; 4 migration (part A without destructive text, part B destructive, migration test); 5 services; 6 tools; 7 prompts; 8 API and frontend; 9 to 12 feature sections, offboarding, retention; 13 config flags; 14 cost; 15 security; 16 tests, SQL check, concurrency check, evals, live test, acceptance; 17 rollout, files, shared-file ownership, work items by wave, model plan; 18 risks; 19 owner questions; 20 review log.
  3. Owner brief written for a voice agent, then owner decisions file, then plan update and re-review of changed areas, then an Opus verifier re-reads the changed areas against the tree before any SQL is written.
  4. Build in waves with gates (detail in Part 3).
- Append a dated entry to `docs/ASSISTANT_BUILD_LOG.md` after every wave, test and stop point. Only you (the orchestrator) write `docs/ASSISTANT.md` decisions log entries.

## Coordination with bdr-94

- Session bdr-94 owns browser v2 (and parked Phase 6). It holds migrations 0192, 0194 and 0195. Excel edit holds 0193. Phase 8 took 0196.
- Load `SendMessage` with ToolSearch, then use `ListAgents` to find bdr-94. If it is not reachable, write the notice as a build log entry and tell the owner.
- At the start, send bdr-94 a short message: you are finishing Phase 8 and starting Phase 9; ask which shared files it still holds and whether it has edits planned in `registry.py`, `loop.py`, `jobs.py`, `repo.py`, `spend.py`, `config.py`, `main.py`, `firewall.py`, `llm_pricing.py`, `AssistantProvider.tsx`, `AssistantConversation.tsx`, `lib/assistant.ts`, `lib/features.ts`, `lib/settingsSearch.ts` or the six locale files.
- Shared-file edits are additive only. Keep bdr-94's blocks intact: `DEFAULT_USER_CAP` 15, `browser_hold_signing_key(_id)`, `assistant_hosted_search_enabled`, `assistant_browser_v2_enabled`, `_validate_browser_v2`, `assistant_daily_cap_usd` 15, the browser routers and firewall prefixes, `EFFECT_SERVER_GATED`, `SERVER_GATED_*`, `FOLLOWUP_*`, `toolset_for(browser=, followup=)`, the hosted `web_search` entry and its price row. Re-read a shared file right before editing it.
- Before committing Phase 8, tell bdr-94 you are about to stage index-only and ask it to pause edits in the shared files until you say done (the 11-02 commit did this; reverse patches handoff was at `/private/tmp/claude-501/browser_v2_handoff`, still present on 2026-11-03 but scratch).
- Announce any migration number you take (expected 0197) before you apply it, and say that 0196 redefines `assistant_spend_reserve` and the kind CHECK.
- Before a live test, post "Notice to bdr-94: live test window open" (build log and message): bdr-94 stays off the triage tables, the e2e accounts and your test port. Post a closing notice when done.
- Other sessions also touch the tree: AI Estimating bdr-84, the urgent worktrees at `~/Desktop/BDR/urgent` (may take port 5071), and the Teams alerts (TA) owner, who owns `graph_email.py`, `email_branding.py` and the bell and routing-grid components. Tell the TA owner before editing those.

## Part 1: close Phase 8

Steps:

1. Read-only inventory: `git status --short` and `git log -3` in both repos; `list_migrations` on dev only; read the build log tail; confirm the facts in "Current state". Confirm B6 exists on dev with a read-only catalog query.
2. Message bdr-94 (see Coordination).
3. P8L-4, owner check. STOP: ASK THE OWNER to open the OpenAI usage page (platform.openai.com, Usage) and look at 2026-11-03 around 04:47 UTC (8:47 PM Pacific on 11-02) for the triage model, and tell you whether a charge shows for that call. Explain in plain words: one AI call was cut off and our books recorded it as free; OpenAI may have charged a fraction of a cent; the question matters because the daily company money cap adds up those book entries.
4. P8L-4, fix decision. STOP: ASK THE OWNER whether to also fix the company cap so a cut-off call can never count as 0. Recommendation: yes, floor the usage where the `llm_call_log` row is written (in `llm._openai_usage` or at the write), limited to cut-off results with zero reported usage, priced like `_floor_cut_off_usage`. Consequence of no: the triage run row is right but the cap can under-count rarely. `llm.py` is a shared file; check with bdr-94 first. If yes, have an Opus subagent make the smallest change plus tests; then mark P8L-4 closed in the build log.
5. "Everything urgent". Spend: Phase 8's 1 USD real-model budget (owner question 22) has about 0.93 USD left (about 0.07 USD spent so far, build log); stop and ask the owner before anything that would pass it. Have an Opus subagent study the golden set misses (e05, s02, c13, c16, c08, t04) and the live step 4 case, and propose a prompt change in `app/assistant/triage.py` `SYSTEM_PROMPT` (around line 201) and, if needed, new golden cases that pin "urgent only when". Run offline `--check` and `--injection` first. Then the live eval: `uv run python -m app.assistant.evals.run_triage_evals --live` with `ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD=1` on the command line. Keep the tuned prompt only if the gate still passes (agreement at least 85 percent, zero escalations on covered negatives, zero invalid answers) and urgent misses drop. If tuning does not help, keep the current prompt and say so; the plan says a gate miss goes to the owner, not a model switch (plan line 782). Do not change `assistant_triage_reasoning_effort` to "low".
6. Evidence for plan 16.2 and 16.3: re-run `scripts/check_assistant_phase8.sql` on dev (rolled-back DO blocks, expect `TEST_RESULT PASS pass=N fail=0`) and `scripts/check_triage_claims.py --user-ids <a>,<b>,<c>`. The claims script needs three distinct active internal profiles and there are only two e2e accounts. STOP: ASK THE OWNER which third test account to use (suggestion: `smoketest-claude@example.com`, whose profile is kept `is_active=false`, so flip it on dev for the run and restore it after, memory `bdr-headless-ui-screenshots.md`). Never pass the owner's own profile id. The script requires no live triage lease, no pending triage item and the dev servers running with `ASSISTANT_TRIAGE_ENABLED` off; it restores the company row itself. Record both outputs in the build log so 16.6 item 2 is evidenced.
7. Gates: Phase 8 test files; assistant sweep `uv run pytest tests/test_assistant_*.py` against the baseline; full backend suite (expect only the 5 known failures; name the 2 flakes if they show up, and rerun them alone); `ruff`; frontend `npx eslint . && npx tsc --noEmit && npm test`; manifest check against `docs/phase8_wi1_manifest.json` (report changes in other sessions' files separately, as the last rerun did for bdr-94's `holds.py`); em and en dash scan of every Phase 8 file; locale key parity.
8. Fix the stale docs listed in "Current state". Update the memory note and the MEMORY.md line.
9. Build the commit plan: a list of every Phase 8 file and, for mixed shared files, exactly which hunks are Phase 8. `bdr_fe/components/assistant/AssistantProvider.tsx` and `AssistantConversation.tsx` are mixed (checked 2026-11-03): both hold Phase 8 "Work on this" hunks (`openPanelWithDraft`, `pendingDraft`) beside browser v2 hunks (`browserHasOpenCredentialCard`), so stage only the Phase 8 hunks. Use plan 17.3 and the `bdr94_held` group in `docs/phase8_wi1_manifest.json` as the list of bdr-94 files that never enter the commit. Browser v2, Excel edit, PM email follow-up, proposal files, `pyproject.toml`/`uv.lock`, `dist/`, egg-info and `docs/AI_ESTIMATING_BUILD_SPEC.md` stay out.
10. STOP: ASK THE OWNER before committing. Show them in plain words: what will be committed (Phase 8 only, local `build` branch, not pushed, not deployed), what stays uncommitted and whose it is, and that nothing goes to prod. Only on a clear yes: pause bdr-94, stage index-only, verify byte for byte, commit in both repos, tell bdr-94 to resume, log it.

Done when: P8L-4 closed or explicitly accepted by the owner; urgent question evaluated and the result recorded; 16.2 and 16.3 evidenced in the build log; gates green except the known failures; stale docs fixed; Phase 8 committed locally (or the owner said not yet); memory updated.

## Part 2: Phase 9 design

Candidate items, each presented to the owner as an option to take or drop:

Items A, B and D are the three items plan 1.2a (lines 60 to 64) defers to Phase 9. C and E are extra candidates the owner dropped from Phase 8 ("Translations English only" and question 1 (a)).

- A. `system` stamp on every background email (plan 1.2a, line 64; brief line 926; Phase 8 question 18 (a) kept it to router emails). `graph_email.send_mail(..., actor_kind=)` already exists (around `app/services/graph_email.py:454-515`) and the `email_log` CHECK already allows `system` (`0172_assistant_phase1.sql:32`). Only the router digest passes it today (`assistant_delivery.py:454-455`). Unstamped background callers: `due_digest.py:415`, `auto_send_scheduler.py:182`, `revision_email.py:169`, `notification_email.py:523`, and `auto_send.py:1302` (verify what `actor_id` is on the scheduled path before stamping). Human `sent_by` callers stay human. `send_draft` paths (`rfq_sending`, `rfq_nudges`, `submittal_*`) write no `email_log` row. These files are on the read-only manifest and TA-owned. Likely no migration unless the owner wants a database guard. Size: 1 to 2 days.
- B. Triage of system-health bells for IT Admin (plan 1.2a, line 63; brief line 468). Exclusion at `app/assistant/proactive.py:277-278` (`BELL_EXCLUDED_EVENTS`, `BELL_EXCLUDED_PREFIXES`) and `bell_recordable` (around 600); routing events in `notification_routing.py:267-296`; test pin `tests/test_assistant_proactive_rules.py:125-127`. Volume: 903 of 1,250 dev bells in 90 days were `tech_outage` or `other`, so the fuse and the daily company cap need re-tuning and a new golden set is needed. Some health messages carry tainted email subject text (`rfp_email_ingest.py` around 4969); packs must stay facts only. Possibly a small migration for new enums. Size: 3 to 5 days.
- C. Translations of the 51 Phase 8 UI strings into ceb, fil, hi, sw and ur (today byte-identical English copies, by owner decision "Translations English only"). Preserve `{{time}}`, `{{decided}}`, `{{flagged}}`; ur is RTL; test pin `bdr_fe/lib/__tests__/assistantInbox.test.mjs:487-504`; its comment at 503 is stale (Phase 7 keys are translated). Locale files are shared with bdr-94: additive only. No migration. Size: about 1 day.
- D. Escalation to a teammate (plan 1.2a, line 62; owner question 7 chose self-only). Blocked by open decision 4 (`ASSISTANT.md:672`) and the rule that no assistant messages another user's assistant (`ASSISTANT.md:144`, the paragraph starting "Proactive alerting is phase 7"). Touches the router (`assistant_proactive.py` `_recipients` around 1021, `_escalation_facts` around 1122, which evaluates facts for the recipient's role), the SQL emit in `0196:606-624` (dedupe key `p8:esc:<inbox_id>` would drop a second recipient), the answer schema and prompt in `triage.py`, templates (`proactive.py:360-386`), and the FE inbox card. Needs a migration. Size: 2 to 3 weeks. Many touched files are bdr-94 shared files.
- E. Stage 2 of triage (unattended prep turn, notes or parked cards). Removed in Phase 8 by owner answer 1 (a). Needs a fourth turn mode (registry and `tests/test_assistant_no_model_unattended.py:87-89` pin three), a message origin CHECK swap (`0192:367`), a conversation surface CHECK swap (`0170:114`), loop branches in bdr-94's `loop.py`, a budget decision, and a fresh security review. Cost estimate in the brief: about 0.22 USD per prep turn, about 6.60 USD a day at 30 runs. Size: 2 to 4 weeks.
- Also bring to the owner: the open decisions in `ASSISTANT.md:667-687` that are still pure owner choices (1, 2, 3, 9, 13, 15, 16, the bell part of 5) and the ones shipped by default awaiting a nod (8, 11, 12, 14, 18, 19). Only decision 4 blocks a build (item D). Also the Phase 8 follow-ups FQ1 to FQ11 (plan 19.1) in case they want to change any.
- Suggested order to recommend: A, then C, then B; D and E only if the owner wants them.

Steps:

1. Recover the Stage 2 text from the transcript (if present) into a scratch note.
2. Run the design workflow (readers, Opus architect, Opus critics, verifiers, apply) and write `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/ASSISTANT_PHASE9_PLAN.md` in the Phase 8 shape. Every item says which files it touches, whether they are bdr-94, TA or read-only manifest files, whether it needs a migration, and its cost.
3. Adversarial review of the plan, findings verified and applied, logged in section 20.
4. Write `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/PHASE9_OWNER_QUESTIONS_BRIEF.md` so that a voice agent whose ONLY source is that file can walk the owner through every question. Copy the Phase 8 brief's structure: instructions for the voice agent (answer only from the file, say "the briefing doesn't say" instead of guessing, dependency map between questions, which answers change the database, suggested order, how to speak: plain words, say "dollars", no code labels read aloud, read back decisions at the end); background; how it works with a worked day; glossary; costs in plain numbers; each question with Question, Why it matters, Options, Pros and cons, Recommendation, Depends on or affects, Likely follow-ups; an answer sheet. The first question is which of items A to E they want at all. Include a question on the live-test budget in dollars (Phase 8 question 22 is the model).
5. STOP: ASK THE OWNER. Tell them the brief is ready, where it is, and that you will wait for their answers. Do not build anything until they answer.
6. Record their answers in `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/PHASE9_OWNER_DECISIONS.md` (answers table with owner notes and "consequences the plan update must carry"). It is the source of truth and overrides the plan.
7. Update the plan (section 19 answers, 20.x change summary), re-review the changed areas, then an Opus verifier re-reads the changed areas against the tree. Re-verify the migration number. Bring any still-open items to the owner.

Done when: plan, brief and decisions files exist; the owner's answers are recorded; the plan matches them and has passed re-review and verification.

## Part 3: Phase 9 build on dev only

Steps:

1. Gate G0a: owner answers recorded and plan updated. Gate G0b: bdr-94 (and the TA owner, for item A) have released the shared files a wave touches, or the wave's edits are additive only and agreed.
2. Wave 0, one Opus work item alone: write `docs/phase9_wi1_manifest.json` (sha256 of every read-only file, extending the Phase 8 list); the migration file in parts if one is needed, its test `tests/test_assistant_migration_0197.py` (or the number you take), and `scripts/check_assistant_phase9.sql` (rolled-back DO blocks, `TEST_RESULT PASS pass=N fail=0`); stubs that raise `NotImplementedError` with final names.
3. Migration number: 0197 was free on disk on 2026-11-03. Re-verify on disk and with `list_migrations` on dev, then announce it to bdr-94 before applying. Apply on dev BDR only: part A, then part B (STOP: ASK THE OWNER to be at the screen if the connector asks for a destructive-text confirm). Reload PostgREST and verify through REST. Run the SQL check.
4. Waves A onward: parallel work items, each file in exactly one item per wave. Sonnet for easy items, Opus for core and security. Each item gets an Opus review and fixes.
5. Gate after each wave: item tests; assistant sweep against the wave 0 baseline; `ruff`; frontend eslint, tsc and npm test; manifest check; em and en dash scan; locale parity. Haiku runs the chores.
6. Concurrency check script if the design has claims, holds or latches (model: `scripts/check_triage_claims.py`), with e2e or throwaway fixtures only.
7. Live eval if the model's job changes (new golden cases, for example for health bells): offline checks first, then `--live` with `ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD` on the command line; its spend counts toward the same live-test budget as step 8.
8. Live test on dev, following the Phase 8 plan 16.5 pattern: post the window notice to bdr-94; precheck (record switch values, spend and ports); seed throwaway data by recorded id; run tick scripts; real-model steps; injection step; time-travel steps; cleanup by recorded ids and restore every switch; count rows before and after. Rules: e2e accounts only; real Teams only to e2e accounts; test email only to `ASSISTANT_PROACTIVE_TEST_EMAIL_TO` with a "[TEST]" subject; port 5074 or 5075 (the plan names 5071, but the urgent worktrees or bdr-94 may hold it; the 11-03 retest used 5074; check `lsof -i :<port>` first); flags on the command line only (`ASSISTANT_ENABLED=true ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED=true` plus a 32+ character `ASSISTANT_TOKEN_SECRET`, `LLM_QUEUE_CLAIM_TYPES` limited to assistant types, every unrelated poller off); `caffeinate -i -s`, no `--reload`; spend: the budget the owner set in the Phase 9 brief (the brief must ask it, as Phase 8 question 22 did; if they did not answer, 1 USD total for the live eval and live test together), enforced by `ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD` on the tick script and a temporary per-day cap on the e2e user, restored at cleanup; scramble the e2e password afterwards. Close the window with a notice.
9. Fix and retest: give each finding an id (P9L-1 and so on), fix through a workflow, rerun the affected steps.
10. Build log entry per wave and test; update the memory note and MEMORY.md index line; orchestrator writes the `ASSISTANT.md` decisions log entry.
11. STOP: ASK THE OWNER before committing Phase 9 locally (same index-only method and rules as Part 1 step 10).

Done when: every chosen item is built, gates pass except known failures, SQL and concurrency checks pass and are logged, live test passes, findings are fixed and retested, docs and memory are current, and the owner has said yes or not yet to the local commit.

## Part 4: prod release (checklist only)

Write this checklist into section 17 of the Phase 9 plan and show it to the owner. Do not run any of it. Nothing touches staging or prod, nothing is pushed and nothing is deployed without the owner's explicit approval given in that session at that moment.

1. STOP: ASK THE OWNER for explicit approval for each environment and each step.
2. Prod backup first (memory `bdr-prod-backup.md`; the owner arms `PROD_SERVICE_ROLE_KEY` and blanks it after).
3. Assistant phases 0 to 8 have never been on prod. With approval, read the prod ledger to list what is missing, then apply every missing file in number order (0154 must precede 0165 and 0170 because it re-lists the `llm_jobs` CHECK; memory `bdr-pm-email-classification.md`). Files owned by other features (0154 to 0158, 0163 to 0169, 0193) need their owners' release decisions too; ask before including them. 0192 and 0194 (browser v2) must be applied or released before 0196, because 0196 redefines `assistant_spend_reserve` and the kind CHECK; coordinate with bdr-94's release. Phase 7: build its indexes concurrently by hand first; apply 0190 A and B and 0191 once. Before applying, check new `llm_jobs` and `bid_split_jobs` check lists are supersets of prod's. One ledger row per file on staging and prod. Dev first, then staging, then prod.
4. 0196 part B, including B6 `assistant_triage_set_enabled`, must be applied on every environment before any backend deploy. The owner may need to be at the screen for part B. Then the Phase 9 migration.
5. Verify each environment with a signature query (md5 over columns, constraints, indexes, function bodies and ACL, RLS, triggers, seeds) matching dev. Reload PostgREST. Smoke each new select live.
6. Gates on the release commit: full pytest, `ruff`, FE tsc, eslint, npm test, `next build`, `run_evals --check`, `scripts/check_eval_report.py`, `check_migration_prefixes.sh`, `check_llm_jobs_constraint_relists.py`.
7. Push both repos (`D1c67/bdr_be`, `D1c67/bdr_fe`); frontend first or together. Railway deploys the backend (check with `gh api repos/D1c67/bdr_be/commits/<sha>/status`, context "lovely-warmth - bdr-api"; push an empty commit if it never deploys); Vercel deploys the frontend. Note the previous Vercel deployment as rollback. Use the `DELETE <route>` probe (405 = route exists, 404 = not).
8. Railway env: assistant flags, `ASSISTANT_TRIAGE_ENABLED=true`, pilot `ASSISTANT_TRIAGE_USER_ALLOWLIST` for one week (one Executive and one engineer of each kind), `FULL_SELF_HOSTED_LLMS_ENABLED` and `ASSISTANT_ALLOW_OPENAI_IN_SELF_HOSTED` handled (triage is OpenAI-pinned), never `ASSISTANT_TRIAGE_TEST_SPEND_CAP_USD`. Railway secrets rotation from the 10-21 incident is still pending; raise it.
9. An Executive or IT Admin turns the company triage switch on (`PUT /assistant/alerts/company/triage`).
10. Smoke: one triage run with cost under the company spend id, fuse not tripped, breaker closed, both uvicorn workers healthy.
11. Watch one week: escalations by severity, urgent at night, cap deferrals, cost. Then clear the allowlist.
12. Merge `main` back into local `build` safely (the 10-22 method) only with approval.

Done when: the checklist is in the plan and the owner has seen it. Execution is a separate decision.

## Reporting

At every STOP, tell the owner in plain words, short and without code:

- Start of session: what you found (Phase 8 built but not committed, one open money-tracking question, the urgent question), and what you will do first.
- P8L-4 stop: what to look up on the OpenAI page, why, and the yes/no fix question with your recommendation.
- After urgent tuning: whether the new instructions made the assistant less trigger-happy, the score before and after, and the cost of the test in dollars.
- Before the Phase 8 commit: what gets saved, what does not and whose it is, and that nothing goes live.
- Brief ready: where the file is, how many questions, how long the walkthrough should take, and that you are waiting.
- After each Phase 9 wave and the live test: what works now, what broke and was fixed, what it cost.
- Before any commit, migration with the owner at the screen, or anything prod: exactly what will happen, what could go wrong, and the question you need answered.
- Always end with the next decision you need from them, or "nothing needed from you right now".
