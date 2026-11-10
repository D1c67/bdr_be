# AI Estimating Build Spec: autonomous quantities and pricing

Status: SPEC, 2026-10-21. Nothing in this document is built yet. It is written for a fresh Claude Code session that will build and test the whole program after the owner approves it, and that has never seen the planning conversation. It supersedes `docs/AI_ESTIMATING_ROADMAP.md` for this build wherever they conflict. It merges nine workstream designs (WS0 to WS8), their critiques (every blocking and major fix is applied here), and the diagnosis of the 2026-10-18 production run on project 7154.

Where this spec cites `file:line`, the line numbers were checked on 2026-10-21. Code moves; re-grep the named function before editing. Where this spec and the code disagree on a fact about today's code, the code wins and you note the difference in the build log.

This file is untracked in `bdr_be` (`git status` shows `??` on 2026-10-21, branch `build` at caaf0cf) and it contains real 7154 dollar figures (sections 1.3, 3.2 to 3.4, WS6, 8.4). Do not commit it as is: keep it untracked, or move the dollar-bearing parts to `EVAL_ROOT/references/7154/` with owner approval first (rule 0.6). Git worktrees created from HEAD do not contain it, so every subagent reads it by its absolute main-tree path `/Users/thomasmoore/Desktop/BDR/bdr_be/docs/AI_ESTIMATING_BUILD_SPEC.md`.

Identifier families used in this spec (each means one thing only): E1 to E25 errors and their acceptance checks (sections 3.3 and 3.5); G1 to G8 root-cause groups (section 3.3); GC1 to GC4 program acceptance checks (section 3.5); ADJ1 to ADJ5 7154 adjudications (section 3.4); PG-A2 and PG-A3 7154 pricing gates (WS6, Q8); PF1 to PF7 7154 page-fact checks (WS1); row ids `C<n>`, `F<n>`, `M<n>` and the rest (section 5.1); Q1 to Q29 owner questions; WS0 to WS8 workstreams; MA to MG migration placeholders. Populations are defined in 8.7 and a short glossary is in 8.8.

---

## 0. How to use this spec

### 0.1 Read order

1. This spec, end to end, once. Sections 4 and 5 are the contracts; everything else hangs off them.
2. `bdr_be/docs/PLANS_READER.md`: section 3 (pipeline: run creation, split, ledger, subs, escalation, main, storage, chat), section 6 (build contract, approved decisions), section 7 (function contracts for runs, agents, chat, routes), section 8 (build log: 8.1 files added, 8.2 files changed, 8.3 where code differs from the design, 8.4 open items, 8.5 self-hosted mode, 8.6 previews). Skip sections 1, 2, 4, 5 unless you need history.
3. The diagnosis facts are embedded in section 3 of this spec (error table E1 to E25, root-cause groups G1 to G8, 7154 ground truth, the acceptance table in 3.5). Every model-facing contract a builder needs is inlined in section 5 (write tools and review schemas in 5.8). The stored 7154 weekend bundle (prod run ff8fad25: outputs, groups, calls, deliverables, the 41-row v1 BOQ, the 54 sub count rows), the nine WS design files and the research notes are data, not text, and they are NOT reproducible without reading prod. Setup step S0 (section 7) already copied them to `/Users/thomasmoore/Desktop/BDR/backups/eval` (`bundles/7154/ff8fad25/` and `proto/`); read them only from there. The WS design files are background: where they differ from this spec, this spec wins. S0 lists the checks that become `skip` if the bundle is lost.
4. `bdr_be/docs/AI_ESTIMATING_ROADMAP.md` only for background and the capability survey. Its decisions 1.1.2 ("no full AI pricing") and 21 ("cost data never reaches a model") are revised (section 2).
5. Memory notes in `/Users/thomasmoore/.claude/projects/-Users-thomasmoore-Desktop-BDR/memory/` that matter here: `bdr-live-llm-test-mac-sleep.md`, `bdr-e2e-api-testing.md`, `bdr-supabase-migration-ops.md`, `bdr-uncommitted-tree-checkout-hazard.md`, `bdr-event-loop-blocking.md`, `bdr-i18n.md`, `bdr-pacific-time.md`, `bdr-plans-reader.md`, `bdr-prod-project.md`, `bdr-headless-ui-screenshots.md`, `bdr-fe-next16-lint.md`, `gotenberg-libreoffice-recalc.md`, `bash-tool-inline-loop-commands.md`, `bdr-failed-to-fetch-means-500.md`.

Bidwright (github braedonsaunders/bidwright, AGPL-3.0) was studied as reference only. Never copy or translate its code. Nothing in this spec is taken from it verbatim.

### 0.2 Operating rules (repeat them to every subagent)

- Migrations go to the dev BDR database only (Supabase project ref `bpidntbyvoooqvaispup`). BDR_Prod (`zxqkqotlcgbalwoqeeca`) and BDR_Staging (`qpfuugcwsdfpxvdgwvwx`) only with explicit owner approval in this session. Never read from or write to prod without that approval, not even a SELECT.
- Apply DDL with the Supabase MCP (`apply_migration`, or `execute_sql` in chunks when the drop-constraint confirm flow fails), then `notify pgrst, 'reload schema';`. Migration file prefixes must be unique; check the highest file in `bdr_be/supabase/migrations` (0162 on 2026-10-21) AND the highest applied on dev (`list_migrations`), since dev may hold unreleased ones. Take the next free numbers at build time for the placeholders MA to MG (section 5.6).
- Preflight before any test, sweep, upload or SQL: `grep '^SUPABASE_URL' /Users/thomasmoore/Desktop/BDR/bdr_be/.env` must show the dev ref `bpidntbyvoooqvaispup` (it did on 2026-10-21). `./switch-db.sh` can repoint `.env` at staging; refuse to proceed on any other ref and ask the owner.
- Test backends: `RFP_INGESTION_ENABLED=false` and every other poller off (section 8.5 has the exact env). Long runs under `caffeinate -i -s`. After a Mac sleep, restart the backend and reset `llm_jobs.attempts` and `next_attempt_at` for the run's jobs.
- One backend at a time on dev during live work. The `llm_jobs` queue is shared: any backend pointed at dev claims every job type (`llm_queue.worker_loop`), so a normal dev backend on :5051 would claim sweep jobs with no ledger and no cassette, and the sweep backend would claim unrelated bidding jobs. Before a sweep: `lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(5051|5061|3000) '` shows only the sweep backend (and the FE if used), and a dev SQL check of `llm_jobs` rows claimed in the last 15 minutes shows no `claimed_by` token other than the sweep backend's. The sweep backend also restricts what it claims (`llm_queue_claim_types`, WS0, section 5.7).
- Never `git checkout`, `git stash` or `git reset` tracked BDR files blind: both trees hold weeks of uncommitted work. Run `git status` in `bdr_be` and `bdr_fe` before anything that touches git. Commit only when the owner asks.
- Never call the sync Supabase SDK inside `async def`. New routes are plain `def`, or use `run_in_threadpool`. Queue jobs are sync worker threads.
- Every internal datetime shown is America/Los_Angeles (`lib/format` COMPANY_TZ on the FE; Pacific in scripts and reports).
- No em dashes or en dashes anywhere: code, comments, prompts, UI strings, docs, commit messages. A grep test (`tests/test_no_dashes_ai_estimating.py`, WS0) enforces it (U+2013, U+2014) on an explicit list of paths: the manifest `bdr_be/tests/ai_estimating_new_files.txt` (one repo-relative path per line, both repos; the integrator appends every file this build creates) plus the `aiEstimating` subtree of every locale file. It does not scan files outside that list (existing files already contain dashes).
- Every new UI string goes into all six locales (`bdr_fe/locales/{en,ceb,fil,hi,sw,ur}/translation.json`; ur is RTL). Translate only new keys: `scripts/translate_catalog.py` today takes only positional locale codes and re-translates whole catalogs (about 5,000 keys per locale); WS8 adds `--only-missing` and `--prefix` before it is run, and the run happens inside a ledger sweep (WS8, $2).
- Before calling a workstream done: backend suite `cd /Users/thomasmoore/Desktop/BDR/bdr_be && uv run pytest -q` with no new failures against the baseline; in `bdr_fe`: `npx eslint .` with no new errors or warnings against the baseline, and `npm run build` clean. Baseline measured 2026-10-21 before any change: pytest 2 failed, 8436 passed (`tests/test_browser_agents_human_api.py::test_models_and_probe_need_the_provider_key`, `tests/test_browser_agents_queue.py::test_hello_reports_the_desired_release`, both outside this build); eslint 0 errors, 2 warnings. Re-run the baseline on build day and record it; "green" means no failure or warning outside the recorded baseline set.
- Pricing data (estimator unit costs, G3 sent amounts, vendor quotes, library values) is internal only. It never reaches the external estimator role, never goes into committed test fixtures (use synthetic numbers), and eval outputs with dollars live under `EVAL_ROOT` outside both git repos.
- Every live model call goes through `app/services/llm.py` (logged, guarded, priced). No direct provider SDK calls from scripts or new modules (a static test enforces it, WS0). Live calls made by scripts (`plans_reader_ab.py`, `review_gauntlet.py`, `translate_catalog.py`, probes) run in their own process and follow ledger rule 2 in 0.4.

### 0.3 How to orchestrate

Use workflows and subagents. Suggested shape:

- One integrator (you, the top session) owns: the migration numbers, every edit to the shared hot files (`plans_reader_runs.py`, `plans_reader_agents.py`, `plans_reader_schemas.py`, `llm_queue.py`, `llm.py`, `RunView.tsx`, `lib/aiEstimating.ts`, the locale files), the build log, and the ledger.
- Subagents build new modules (new files) in parallel, each from its workstream section in section 6, against the contracts in section 5, with fakes for anything not yet built. Each subagent returns: files created, tests added and their result, open issues, and the exact hook edits it needs in shared files (as a patch description). The integrator applies hook edits serially.
- Model choice for subagents: Sonnet for mechanical tasks (fixtures, locale additions, test scaffolds), Opus 5.5 for algorithm and integration work.
- Parallelism by wave is in section 7. Within a wave, workstreams that only add new files run fully in parallel.
- Worktree isolation: git worktrees start from HEAD and do NOT contain uncommitted work, including this spec and the roadmap (both untracked). Give every subagent the absolute main-tree path of this spec and of `EVAL_ROOT`. First run `git status` in both repos. If either is dirty, ask the owner whether to commit the current tree to a branch before you create worktrees. If the owner says no, work in the single tree and rely on file ownership (each subagent writes only its own new files; shared files only through the integrator).
- Keep hook edits small and named (one helper call per hook) so serial merging is cheap.

### 0.4 The $400 live spend ledger

The owner capped live model spend for building and testing at $400 total, dev only. Deterministic tests, replays of stored outputs and cassette replays are $0. Rules:

1. WS0 builds the ledger first (migration MA, `eval_ledger.py`). No live call is made before the ledger enforces on the sweep backend (`AI_EVAL_LEDGER_ENFORCE=true`).
2. Every live call happens inside an open sweep (`scripts/eval_ledger.py open --workstream WSn --purpose dev_probe|smoke|core|holdout|noise|ab --budget <usd>`). One open sweep at a time, and one live process per open sweep (the in-flight reservation is in-process, so two processes on one sweep could overspend it). Close it after. Attribution outside the sweep backend: every script that can make a live call takes `--sweep <id>` (default: the single row of `ai_eval_sweeps` with status open; refuse when there is none), sets the process setting `eval_sweep_id` before its first call, and runs with `AI_EVAL_LEDGER_ENFORCE=true`, so the guard in `llm._guarded` and the stamp in `llm_gate.log_call` apply exactly as in the sweep backend. Never run a live script while the sweep backend is processing jobs of the same sweep.
3. Every model used live needs a verified `llm_pricing.PRICES` row first (the guard refuses unpriced models). Re-check Anthropic and OpenAI prices on the provider pricing pages on build day (use the claude-api skill for Claude) and write the check date in a comment.
4. Before a live run, dry-run its estimate (`GET /ai-estimating/runs/{id}/estimate`, `scripts/eval_run.py --dry-run`). The dry run includes splitter cost for unlabeled drawing files (the splitter is an LLM job and the guard covers it) and the post-run review budget.
5. Allocation (section 8.4): $108 for workstream probes, $278 shared integration and acceptance sweeps (sized from the per-case table in 8.4), $14 contingency. Contingency is released only by `allocate` with a note visible to the owner.
6. Cassette modes (`LLM_CASSETTE_MODE`, WS0): `off` (no cassette); `record` (always call live and store the response; never replays, so it measures noise); `replay` (hit or `CassetteMiss`, never live, $0); `record_missing` (replay on hit, live on miss and store). Purposes: `noise`, `ab`, `smoke`, `core`, `holdout` use `record`; `dev_probe` uses `record_missing` (re-running an unchanged prompt costs $0); every $0 suite and CI uses `replay`.
7. Stop conditions (stop, write the report, ask the owner):
   - global spend reaches $360: only smoke or holdout sweeps may open after that;
   - a sweep exceeds its budget by more than 15% (an estimate or guard bug: fix with replay before spending more);
   - the same blocking acceptance check fails on two consecutive integration runs of the same case (fix at $0 with replay first);
   - after the core sweep, median absolute bid error is above 20% (twice the target): report before running the holdout;
   - remaining budget is below the next planned sweep's dry-run estimate plus $10.

### 0.5 When to stop and ask the owner

- Any contact with BDR_Prod or BDR_Staging (reads included), any prod env change, any deploy.
- Marking 7154 adjudications or case aliases `reviewed: true` (section 8.2).
- Any spend beyond $400, or releasing contingency for something not listed in section 8.4.
- Re-running the holdout sweep (it runs once).
- Uncommitted work in a tree before creating worktrees or branches.
- A design question not answered by section 2 (use the stated default if the owner is unavailable and record it).

### 0.6 What to report at the end

A single build report (also appended to `PLANS_READER.md` section 8 as a new build log entry, no dollar figures from real projects in the repo copy):

1. Per workstream: status (done, partial, deferred), files added and changed, migrations applied to dev with their final numbers, flags and their dev values.
2. Test totals: backend suite count and result, FE eslint and build result, new tests per workstream.
3. Acceptance table E1 to E25 plus GC1 to GC4 on the 7154 smoke case (pass, fail, safe refusal, skip) with measured values, in the row order of section 3.5.
4. Golden results: smoke, core and holdout tables (bid error vs G3 sent, hours error, count lines exact, length bands, dollar coverage), the $0 LOPO pricing backtest, refusals and why.
5. Spend: ledger report by workstream and sweep, total, remaining.
6. Model choices per step with the evidence (A/B tables).
7. Deviations from this spec and why; owner questions answered by default; open items.
8. Updated memory notes (one feature note "BDR AI Estimating Autonomous Build", DEV ONLY until released).

---

## 1. Goal, definition of done, and what "humans verify price" means

### 1.1 Goal

From a bid set (drawings, specs, RFP or owner exhibits, addenda, and vendor quotes when G3 has them), the system produces, without a human in the loop:

- quantities in G3's BOQ vocabulary, every line with a stated basis (counted, schedule, measured, assumed route, census floor, stated, policy) and evidence (marks on plans, polylines with feet, cited sources);
- a scope decided by G3 policy (bid conditions such as prevailing wage, exclusions, standard allowances, company standards, party attribution), written as proposal-style clarifications instead of RFIs;
- a priced estimate: labor hours from G3's own history, material from G3's history or vendor quotes, and Verify-style totals exactly as G3 computes them today (rate by wage type, hour round-up, 8.375% tax, markup, quote replacement, every non-quoted material line carried);
- an independent check (history bands, deterministic rules, reviewer model, one bounded revision) and integrity gates that refuse to call an estimate ready when evidence is incomplete.

A human verifies the price before anything is sent. Build the full pricing capability now; it will be tuned before anyone relies on it.

### 1.2 Definition of done for this build

The build is done when all of these hold:

1. All workstreams in section 6 are built behind flags (parent flags off in code, on in dev; section 5.7 lists the child flags that default true under a false parent), with their deterministic tests green, the backend suite green against the baseline in 0.2, FE eslint and build clean against the baseline, six locales in parity for new keys, and zero em or en dashes in the files of the no-dash manifest.
2. The $0 suites pass: scorer v2 on the stored 7154 bundle (17 of 22 count lines exact raw), the acceptance detector self-test (each E check FAILS on the stored weekend bundle with the baseline value in section 3.5), the 7154 replay checks for every row of 3.5 marked replayable, the LOPO pricing backtest (section 1.3 item 4), and the classifier gate over the 66 golden estimator BOQs (WS0).
3. The live golden program in section 8 has run within the $400 ledger: integration on 7154, smoke (3 cases), noise repeat, core (5 cases, or fewer only by the pre-named drop order in 8.4), holdout (4 cases, once, frozen code).
4. The program targets in section 1.3 are met, OR the report states exactly which are missed, by how much, and why, with the stop condition that ended the run. Tuning stops at the holdout.

### 1.3 Golden acceptance numbers (program level; WS0 measures them)

Truth: what G3 sent is the primary price truth. For 7154 that is $117,459.16 (625 h at $125, Graybar lighting quote). The Verify-leak-corrected total ($121,760.63, section 3.4) and the estimator total ($117,758.36) are reported alongside. Quantity truth is the estimator BOQ with reviewed adjudications.

1. 7154 smoke (training case, adjudicated truth): count lines exact >= 21 of 23 (22 raw count lines plus adjudication ADJ1, the 4 GFCI breakers); all 6 length lines `acceptable` (within 25%) and at least 4 `good` (within 10%, EMT must be good); the demolition track line must be a MEASURED length on 7154 (the human-routed block finding WS3 allows elsewhere counts as a fail here, see E15 in 3.5); dollar coverage >= 0.85; dollar-weighted error <= 0.15 (both defined in 3.6); bid within 10% of $117,459.16 (target 5%); every row of section 3.5 marked "gates 7154 smoke" is `pass` (a `safe_refusal` counts as a fail for 7154 smoke; rows marked `skip allowed` may skip only for the reason stated there). Hours: the gating hours check on 7154 is PG-A2 (WS6, Q8); the engine's own hours (PG-A3) are reported against 625 and judged by the WS6 LOPO hours metrics, not by a 7154 threshold.
2. Smoke plus core (all 8 cases have G3 sent truth and a committed Verify, section 8.1): median absolute bid error vs G3 sent <= 10%; at least 60% of cases within 10%. Refused cases count as misses in both statistics. At most 1 refusal among the 8 (among the cases run, if the pre-named drop order in 8.4 removed any) and 0 among the 3 smoke cases. A refusal must cite a gate code that matches a real defect in the reference by the mapping in 3.6 (GC3); otherwise it is scored `false_refusal`. No unrefused case worse than 25%. No regression on any count kind beyond the noise floor (3.6: regression vs the old-pipeline bundle of the same case; noise floor from the two 7154 runs; cases without an old-pipeline bundle skip this check).
3. Holdout (4 cases, run once at go/no-go against a frozen, clean code version): median absolute bid error <= 12%.
4. LOPO oracle pricing backtest ($0, the LOPO population of section 8.7, 35 cases on 2026-10-21 before the reconcile filter; estimator quantities priced by the engine with the case's own project excluded from the library): median absolute error vs G3 sent <= 7%, at least 70% within 10%.
5. 7154 is a training case: every workstream tunes aliases and regexes on it. Only core and holdout measure generalization. Live sweeps are leave-one-project-out too: the pricing library, scope mining, commonly-missed list and history bands exclude the run's own project through `plans_reader_runs.eval_case_id` (section 5.7).

Gates the program must also pass: security matrix (estimator role gets 403 or 404 on every AI Estimating and pricing route; Accountant never receives a money field from any route, chat tool or thread read); no group exceeds its loop budget by more than 15%; no billed call recorded with a NULL cost.

### 1.4 What "humans verify price" means in the product

- The priced estimate is an internal draft. It lives in the run's Pricing tab and the priced workbook ("INTERNAL - AI PRICED DRAFT - NOT VERIFIED" banner), visible only to pricing roles (section 5.5).
- The run header shows an autonomy banner: `ready_for_verification`, `needs_review`, `blocked`, `in_progress` or `not_evaluated`, with the failing checks as chips (integrity gates, lengths produced, scope conditions, pricing coverage, independent review, history bands, evidence).
- Every BOQ line carries a quantity chip (how the number was made) and, for pricing roles, a price chip (quote, library median n=, policy, missing). The Evidence tab shows marks and polylines on the plans so the verifier checks evidence, not arithmetic.
- A verifier in VERIFY_ROLES (Executive, IT Admin) records a sign-off: `verified` or `rejected` with a note, tied to one priced version, storing the autonomy and totals snapshot. `verified` requires `ready_for_verification` or an explicit note per accepted failing check.
- Nothing is sent, nothing is written into the project's Verify step, proposals, RFQs or auto send. A later "copy to Verify" action is an owner decision after tuning (Q19).

---

## 2. Owner decisions and open questions

### 2.1 Final owner decisions (binding)

1. Goal is fully autonomous estimating: quantities and a priced estimate (labor hours, material, Verify-style totals). Humans verify the price before anything is sent.
2. Giving the models the correct tools is the core fix: a bounded, schema-typed tool loop inside our group kinds, with mostly deterministic tools, under the spend cap. Code computes every number it can; models make judgments and call tools.
3. Any model provider is allowed now. Choose per step on evidence (section 8.3).
4. Not a 26-week build. One build session with subagents, right after approval.
5. Live spend for building and testing is capped at $400 total, dev only.
6. Roadmap decision 1.1.2 ("no full AI pricing") and decision 21 ("cost data never reaches a model") are REVISED: pricing is in scope; pricing data may be used by internal tools and models (aggregates only: medians, quartiles, n, month; never project names, GC names or per-project rows); it stays internal-only and never reaches the external estimator role.
7. Standing rules: dev DB only; never prod without explicit approval; no em dashes; six locales; Bidwright reference only.
8. Prod keeps `AI_ESTIMATING_STATE=test` (Executive and IT Admin only) and every new flag off until a release the owner approves (section 9).

### 2.2 Open questions (the builder uses the default when the owner has not answered; record which defaults were used)

| # | Question | Default |
|---|---|---|
| Q1 | Who reviews the 7154 adjudications (ADJ1 count the 4 GFCI breakers, ADJ2/ADJ3 remove the EM battery premium, ADJ4 Verify-leak-corrected total) and the SHADED = EM alias? | Owner or Estimating Admin, about 15 minutes. Until then scores show raw and adjudicated values and acceptance uses adjudicated marked `unreviewed`. |
| Q2 | Approve the fixed golden tiers (section 8.1), holdout frozen now? | Yes. |
| Q3 | Approve the $400 split (section 8.4)? | Yes; rebalance through the ledger with notes. |
| Q4 | Primary price truth = what G3 sent; bid bands 10% (target 5%) on smoke and core, 12% median on holdout? | Yes. |
| Q5 | Labor rates: prevailing wage $125, non-prevailing $100, per-GC overrides ($120 for Monument and Sletten seen in notes)? | Yes, editable by Executive and IT Admin. Overrides are stored in `ai_pricing_settings.policy.rate_overrides` keyed by `general_contractors.id`; a run's GCs are the project's `project_gcs` rows (`project_id`, `gc_id`); a run with no project has none. When a bid goes to several GCs with different overrides, use the default rate and add warning `multi_gc_rate`. |
| Q6 | Material markup 10% every section, labor markup 0 (rate is loaded)? | Yes. |
| Q7 | Pricing viewers: Executive, IT Admin, Estimating Admin, both Estimating Engineer roles; Accountant excluded; external estimator always excluded? | Yes. |
| Q8 | 7154 pricing gate: judged with G3's recorded 625 hours (PG-A2) or must the hours policy also hit it (PG-A3)? | PG-A2 is the gate (within 3% of the ADJ4 total); PG-A3 reported; the hours policy is judged by the WS6 LOPO hours metrics and the golden Verify band, never by a 7154 hours threshold (section 1.3 item 1 follows this). |
| Q9 | With no human-selected quote, pick the lowest eligible quote or a preferred-vendor list? | Lowest eligible, alternatives shown (7154 picks Codale $17,362; G3 chose Graybar $17,550). |
| Q10 | Allowances in Verify: untaxed, in the materials section, with the materials markup? | Yes. |
| Q11 | Approve hospital-grade seed prices ($32.00 duplex, $44.20 GFCI, from the 7154 estimator) until real observations exist? | Yes, labeled `policy` on every line that uses them. |
| Q12 | Cutting, patching and painting assigned to the EC by the drawings (7154 E2.01 note 5): exclude? | Yes, hard exclusion with a clarification line (G3 did so on 7154). |
| Q13 | Wage type when neither the project record nor the documents decide? | Prevailing wage with `needs_review: wage_unknown` (62 of the 77 backup projects with `projects.wage_type` set were PW). |
| Q14 | Carry thermostat relocations and #18/4C control cable even when mechanical notes assign ATC wiring to controls? | Yes, with the clarification "Thermostat relocations and control cable included; ATC devices and programming by others." |
| Q15 | G3 branch convention: one circuit per 3/4" EMT home run carrying H+N+G, #12 minimum; power home runs sized from the panel schedule wire column or a 3% voltage drop on load current; LIGHTING circuits on #12 unless voltage drop on load current exceeds 3%? | Yes. This matches the 7154 estimator (3#12 for all lighting although the schedule shows #10 on 1HD-21) and the 3.00 median wire-to-conduit ratio over 34 golden estimates. |
| Q16 | Code owns branch wire and conduit rows (no side-by-side period with model lengths)? | Yes. |
| Q17 | Vertical drops and panel rise? | 0 until calibration on the tagged training split says otherwise; the owner signs off the calibrated conventions version. |
| Q18 | Auto-relabel a document whose numbered sheets are >= 80% another discipline? | Yes in dev; decide for prod at release. |
| Q19 | Should a verified AI price flow into the project's Verify step? | Not in this build. |
| Q20 | Who may override a blocking gate? | Estimating Admin, Executive, IT Admin with a written reason of at least 10 characters; engineers may request a recount (in dev) but not override. |
| Q21 | Review revisions: apply automatically before the verifier sees the price, or only propose? | Apply in dev; prod `shadow` (computed and shown, not applied) until the review acceptance passes. |
| Q22 | May Anthropic routes run for plans steps while `FULL_SELF_HOSTED_LLMS_ENABLED` is on (today only OpenAI is exempted for plans features)? | Yes in dev. Prod follows the A/B results at release with explicit approval. |
| Q23 | Anthropic refusal fallback (`fallbacks: "default"`, may answer on another Claude model at that model's price)? | On, priced by the model that answered. |
| Q24 | Who may activate a scope policy version? | Executive and IT Admin activate; Estimating Admin edits drafts (only reachable when `AI_ESTIMATING_STATE=live`). |
| Q25 | Scope clarifications as suggested proposal lines (human approves before send)? | Yes, stored as suggestions; nothing is written to proposals in this build. |
| Q26 | Who may sign off an AI price? | VERIFY_ROLES (Executive, IT Admin). |
| Q27 | May trimmed JSON outputs of 7154 run ff8fad25 (no drawings, no prices) be committed as test fixtures? | Yes, under `bdr_be/tests/fixtures/plans_reader/run_7154/`. |
| Q28 | Keep page facts across runs keyed by file sha256? | Yes. |
| Q29 | Marked plans: keep letter pages and add the full-size annotated PDF? | Both. |

---
## 3. Current system and the 7154 diagnosis

### 3.1 System map (bdr_be unless noted; lines checked 2026-10-21)

Run state machine, `app/services/plans_reader_runs.py`:
- `RUN_STATUSES` :82; `PIPELINE_KINDS = ("sub","rollup","escalation","main")` :97 (drives `groups_total` and the "any group active" checks); `MAIN_PASS = 1`, `MAIN_RETRY_PASS = 2` :100-101; `REFUSED_PROJECT_CATEGORIES` :142 (answer-file blindness); `RECOVERY_AFTER_SECONDS = 600` :162; `_cas_status` :299; `_touch` :334 (run heartbeat is `updated_at`).
- `_settings_snapshot` :465 (no prompt hash, no code version today); `create_run`; `_labeled_page_meta` :873-883 (labeled sets get `derived_page_meta`: title None, role `other`); sheet sizes stored :886-907.
- `on_split_file_done` -> `_maybe_finish_splitting` :1275-1301; `_plan_reading` :1324-1460 (downloads every document, plans groups; file chunks :1381-1400); `_advance_splitting` :1923-1964.
- Escalation: `_escalation_tiles` :1653, `_escalation_rows` :1679 (tiles from model-guessed region strings).
- Merge: `_merge_outputs` :1753 (concatenation, no dedupe); `_category_output` :1770 (prefers the LLM roll-up, attaches escalation outputs whole as `escalation_patches` :1798-1825).
- `_advance_reading` :1967 (queues a roll-up :1989-2004); `_write_outputs_and_queue_main` :2018-2061; `_advance_consolidating` :2064 (any pass >= 2 is a retry); `_queue_main_retry` :2116; `_finish_with_main` :2192-2206 (writes main output, renders, closes the run); `advance` :2244; `deliverable_spec(kind, main)` :2536 (no sb or run); `_DocBytes` :2684; `_render_bytes` :2742; `render_deliverable` :2808; `render_all_deliverables` :2868; `_rerender_source` :2908-2916; `claim_change_slot` :2964; `_self_heal` :3187-3204 (only on GET poll); `get_run_payload` :3262. `_write_output` :1835-1849 hard-codes version 1 and swallows unique violations.

Agents, `app/services/plans_reader_agents.py`:
- `GROUP_KINDS` :95, `FEATURE_BY_KIND` :96, `SCHEMA_NAME_BY_KIND` :103, `CHARS_PER_TOKEN = 3` :118, `_DATA_NOT_INSTRUCTIONS` :139, `_NO_PRICING` :147, `SUB_SYSTEM` :156, `ROLLUP_SYSTEM` :292, `ESCALATION_SYSTEM` :309, `MAIN_SYSTEM` :335-395 (line 362 "list the branch breakers of every panel"; 363-365 branch wire measured by the model; 375 extra allowances).
- `run_doc_order` :482, `split_sheet_title` :549, `derived_page_meta` :578-603, `build_ledger` :635 (project select only name, number, address :651-659; legend only when single `page_role` is legend_symbols or general_notes :693-718, check at :707), `ledger_text` :741-817, `plan_ledger` :820-897, `spec_toc_from_output` :907-940.
- `_add_document_pages` :1120-1186 (any item kind other than `file` is treated as an image page); `record_call` :1252; `_call_model` :1306 (one `plans_reader_calls` row per call); `expected_output_tokens` :1408; `_finalize` :1468; `_build_sub` :1503-1553; `_build_rollup` :1573; `_sheet_rows` :1617; `_build_escalation` :1629; `_build_main` :1719-1770; `_BUILDERS` :1764, `_SYSTEMS` :1770; `_ACTIVE_RUN = ("reading","consolidating")` :1788; `_pause_for_cap` :1792; `run_group` :1803 (kind must be in `_BUILDERS` :1817; cancels on terminal status :1826-1831; `check_cap` :1837; est_usd :1834; schema choice `MAIN_AGENT_SCHEMA if kind == "main" else SUB_AGENT_SCHEMA` :1877); modify path :1963-2036; `render_pending_spec` :2059.

Schemas, `app/services/plans_reader_schemas.py`: header "No schema carries a price" :21; `PAGE_ROLES` :46-57; `ESTIMATE_ALLOWANCES` :118 (9 rows); `RESOLUTION_FLAG` :185; `SUB_PAGE` :195-205; `COUNT_ITEM` :207 (basis `counted_symbols|schedule_text|keyed_note|stated_quantity|inferred`); `RUN_ITEM` :222-237; `SCHEDULE_ITEM` :263-273; `QUESTION_ITEM` :285; `ESCALATION_SCHEMA = SUB_AGENT_SCHEMA` :305; `BOQ_ITEM` :310-321; `PLAN_MARK`, `LEGEND_ROW` :360-384; `MAIN_AGENT_SCHEMA` :419; `DELIVERABLE_SPEC_SCHEMAS` :432; `CHAT_TOOLS` :450; `validate_strict` :503.

Render, `app/services/plans_reader_render.py`: `boq_quantity` :133 (+10% on FT rows with `wastage_applies`); `_CONDUIT_RE` :156; `conduit_fittings` :176-206 (EMT connectors ceil(L/50), couplings ceil(L/10), straps = couplings, anchors and screws ceil(straps/2), boxes ceil(anchors/5)); `other_conduit_accessories` :209-222; `RECAP_SUMMARY_ROWS` :690; `_EstimateSheet.item` :741-764 (H to O empty); `subtotal` :775; `allowance` :796; LABOR RATE label :851; `_write_recap` :981 and :993-1046 (dynamic layout, summary values in column E); `render_estimate_xlsx` :1121; `render_markup_pdf` :1591.

Other:
- `app/services/llm.py`: `LlmUsage` :130; `_plans_feature` :217 (plans_* pinned to OpenAI, `openai_plans_model` config :287); `_anthropic_usage` :638 (folds cache writes at 1x); `_anthropic_call` :681 (text only); `_guarded` :802 with `_log` :825; `_parse_tool_calls` :1127; `_raise_if_unusable_parts` :1181; `complete_parts` :1208 (OpenAI Responses only; tools, previous_response_id, strict json_schema; renews the queue lease before sending :1309-1314). Module docstring :1-20: in full self-hosted mode only OpenAI plans features may leave the box. `class _Feature` :196 (field `openai_only` :205); `_FEATURES` :229 registers `plans_sub`, `plans_main`, `plans_modify`, `plans_chat` at :297-300, all from `_plans_feature()` with `openai_only=True`; `FEATURE_BY_KIND` (`plans_reader_agents.py:96`) maps sub, rollup, escalation -> `plans_sub`, main -> `plans_main`, modify -> `plans_modify`.
- `app/services/llm_gate.py`: `log_call` :201 writes the `llm_call_log` row (insert at :253; never raises). `app/services/llm_errors.py` imports `openai` (:31) and `anthropic` (:120, :133) for error classification. `app/services/llm_health.py:196` imports `openai`.
- `app/services/plans_reader_images.py`: `pdf_chunks` :429 (there is no `pdf_chunks_for_pages` today).
- Existing live harness `scripts/plans_reader_live_run.py`: signs in as the dev E2E account (`bdr.e2e.electrical@g3test.dev`, password set through the service role and scrambled after), creates a run, uploads a backup project's `drawing`, `electrical_drawing`, `specification`, `addendum` and `other` files from `BDR_Prod_2026-10-01` (with `--labeled`: electrical_drawing -> electrical_drawings, specification -> specifications, addendum -> addenda, other -> other; plain `drawing` files go unlabeled to the splitter), prints the estimate, starts, polls, downloads deliverables and scores with the golden script. Flags: `--project-prefix`, `--base-url`, `--out`, `--labeled`, `--render-mode adaptive|tiles_260`, `--poll-seconds`, `--max-minutes`, `--skip-specs`, `--run-id` (attach). It has no `--no-start`.
- `scripts/translate_catalog.py`: positional locale codes only (`translate_catalog.py hi ur`), re-translates the whole English catalog through the `translate` feature.
- `projects.labor_time` is the enum `day_work | night_work` (`0023`); `projects.wage_type` is `prevailing_wage | non_prevailing_wage`.
- `app/services/browser_agents/llm/__init__.py:81-90` builds raw OpenAI and Anthropic clients outside llm.py (allowlisted; `BROWSER_AGENTS_ENABLED=false` in tests).
- `app/services/llm_pricing.py` :61 `PRICES` has only `gpt-6-astra` ($10 in, $1 cached, $50 out per M; 2x input and 1.5x output over 272k). Unpriced models log NULL cost.
- `app/services/ai_spend.py`: `CALLS_TABLE` :73 (sums `plans_reader_calls`), daily cap default $75 :79, `_spent_since` :216, `check_cap` :297, `after_call` :341. No run or group ceiling.
- `app/services/llm_queue.py`: job types :102-136, `NON_LLM_JOB_TYPES` :132 (NOT a claim pass), `_JobSpec` dataclass :194 (factory `_spec(job_type)` :236), `_claim_plans` :815-840 (batch jobs get total-1 slots), `_claim_tick` :1000-1047, `renew_lease` :1066. CHECK last redefined in `0154_pm_email_classification.sql:70-72` (read it live).
- `app/services/plans_reader_chat.py`: `MAX_ROUNDS = 8` :82, per-tool 200k and per-round 300k char caps :91-95, `_CHANGE_WORDS` :157, `get_thread` :314-332 (select *), `run_message` :670 (interrupted message is failed :713-739), `_tool_loop` :856, `chat_tools(ctx)` :1028, `_SYSTEM_RULES` :1195 ("Never give prices, costs, labor rates..." at :1202), dispatch dict :1302, `_acting_user` :1687.
- `app/routers/plans_reader.py`: router deps `require_plans_reader`, `require_internal`, `require_ai_estimating` :82; estimate endpoint `estimate_run` :507 (its `cost_usd_max` is computed in `plans_reader_runs.estimate` :757-872); `get_output` :675 (serves only main or a category); `list_threads` :946 and `get_thread` :962 (require_internal, includes the Accountant); `send_message` :977 (require_writer).
- `app/routers/pricing.py`: `DEFAULT_TAX_RATE = 8.375` :57, `apply_tax` :66 (cents ROUND_HALF_UP), `_materials_rows` :106, `section_summary` :212, `_verify_originals` :251, `pricing_summary_numbers` :279, `VERIFY_NUMBERS` :513 (labor_amount, materials_amount, gear_amount, underground_amount, low_voltage_amount and the five *_markup_amount).
- `app/core/roles.py`: `INTERNAL_ROLES = WRITER_ROLES | {ACCOUNTANT}` :36, `VERIFY_ROLES = {EXECUTIVE, IT_ADMIN}` :40. `app/core/features.py:62-66`: `AI_ESTIMATING_STATE=test` admits only Executive and IT Admin (dev `.env` line 216 is `test`).
- `app/core/config.py`: `plans_reader_group_input_tokens = 240000` :293, ledger budget `plans_reader_ledger_max_tokens` :296, escalation fraction :304, `plans_reader_tile_ppi = 260` :306, call timeout 600 s :319, `llm_queue_worker_concurrency = 3` :355, lease 900 s :358, timeout-vs-lease validator :1590-1604.
- Migrations: `0152_plans_reader.sql` (group kind CHECK :151-170 = sub, rollup, escalation, main, modify, chat; run status :58-59; calls :180-205, indexed only on (run_id, created_at); outputs scope CHECK `('category','main')` :209-220, unique index `plans_reader_outputs_uq` (run_id, scope, coalesce(category,''), version) :220; deliverable kinds :227; RLS enable and force, no policies :308-327). `0161_file_access_events.sql:28-36` source CHECK (7 values; `file_access._insert` swallows a CHECK violation).
- Golden scorer: `scripts/plans_reader_golden.py` (`normalize_description` :170, `_KEY_PATTERNS` :227, `_TAG_RE` :247, `_MODIFIERS` :252, `key_tokens` :275, `similarity` :316, `parse_estimator_boq` :381, `rows_from_spec` :465, greedy one-to-one `match_rows` :537-592, threshold 0.72 :51, `EXACT_TOLERANCE = 0.02` :52 relative, used at :600); module-level helpers the identity functions depend on: the `BOQ_GROUPS, BOQ_LEVEL0, BOQ_SUBGROUPS, UNITS` import :44-49, `_KNOWN_SUBGROUPS` :60, `_SUBGROUP_PARENT` :64, `_ACCESSORY_WORDS` :69, `_CAVEAT_WORDS` :72, `_UNIT_ALIASES` :75, `class BoqRow` :89; `tests/test_plans_reader_golden.py` collects 25 tests and also uses `golden.main`, `report_markdown`, `golden.BoqRow` and `golden.similarity`.
- `app/services/auto_send_estimate.py`: `parse_recap` :353 (TOTALS row only).
- FE: `bdr_fe/components/aiEstimating/RunView.tsx:86` `RUN_TABS = ["brief","outputs","deliverables","calls"]`; `DocumentTree.tsx`; `bdr_fe/lib/aiEstimating.ts`; `bdr_fe/lib/features.ts` (fail-closed map).
- Dependencies: pypdfium2 5.13+ (installed 5.13.0, which has `PdfObject.get_bounds()`; every PDFium touch under `pdf_split._PDFIUM_LOCK`), pypdf 6.14.2, anthropic 0.105.2, openai, openpyxl. Not present: jsonschema, rapidfuzz, scipy.

### 3.2 The 7154 run that was diagnosed

Run ff8fad25 (PROD, 2026-10-18), project 7154 UMC Outpatient Pharmacy Remodel, project id `7dfc64e6-36ef-4c67-9d02-0d7942015011`, number 26.8.7154, 1800 W. Charleston Blvd., prevailing wage, union, public_other, day work. Model gpt-6-astra, medium effort, adaptive render, tile ppi 260, group input 240k. Cost $16.56 actual ($21.95 estimated): sub 5 calls 770,432 in / 79,551 out $11.62 (three electrical subs about 219k input each at $2.62 to $3.20; specs sub 101,470 input, $2.30); main 85,502 / 26,924 $2.20; roll-up 43,678 / 34,488 $2.16; escalation 3 calls 32,168 / 3,951 $0.49; chat 2 calls $0.09. Cached tokens barely hit (0 to 3,618 of about 219k).

Documents: ELEC drawings (10 pp, 42x30, vector, full text layer), MECH drawings (8 pp, labeled electrical_drawings), SPECS (166 pp), Tech Specs 1 (2 pp), UMC Door Spec Guide (2 pp), Exhibit A Scope of Work (6 pp, rfp). All six inputs and both vendor quote PDFs (Codale, Graybar) are in the local backup `/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01/storage/project-files/7dfc64e6-36ef-4c67-9d02-0d7942015011/`, so 7154 can be re-run on dev without prod access (build step S2).

File-to-label map for every 7154 dev run (it reproduces the prod labeling exactly, so E24 reproduces). Prod run ff8fad25 had: ELEC and MECH labeled `electrical_drawings`; the three spec PDFs labeled `specifications`; Exhibit A unlabeled, classified `rfp` by the splitter. The backup's `project_files.category` values give the same result through `plans_reader_live_run.py --labeled` (and `eval_run.py`, which uses the same mapping):

| Backup path (under the project folder above) | project_files.category | Run label |
|---|---|---|
| `drawing/c85a4720840d49a288d9d0c7d3c87fed-Reference Material - Architectural - ELEC_RESPONSE_TO_CLV_COMMENTS D1.pdf` | electrical_drawing | electrical_drawings |
| `drawing/9466b0a7b03b42c4bd9bf8feac10c9e0-Reference Material - Architectural - MECH_RESPONSE_TO_CLV_COMMENTS D1.pdf` | electrical_drawing | electrical_drawings (WS1 relabel moves it to mechanical when `plans_reader_facts_relabel` is on) |
| `drawing/9469e88285a6412b96509d250f8f6b4c-Exhibit A - Scope of Work.pdf` | drawing | none (splitter; prod classified it `rfp`) |
| `specification/47924a195f3c4525b6e6abd57c17b8a6-Reference Material - Architectural - SPECS.pdf` | specification | specifications |
| `specification/aed667ca84774c63aeda6cb8ce4f40cb-Reference Material - Tech Specs 1.pdf` | specification | specifications |
| `specification/92083b13b1a74e8da33e43a9b30e9308-Reference Material - UMC Door Spec Guide 2025.pdf` | specification | specifications |
| `quote/54c640bb08ee44ae897d3880f1fe9dc5-Codale Lighting Quote.pdf`, `quote/99c48c6555994002a6fab5a37506a582-Graybar Lighting Quote.pdf` | quote | never a reading input; uploaded through the pricing quote endpoint (WS6) |

Note the backup stores the ELEC and MECH sets in the `drawing/` folder although their category is `electrical_drawing`: always map by `data/project_files.json` category, never by folder.

Output: 41 BOQ rows in 1 area, no WIRING rows, L5 as 7 EA, track as 3 EA, 26 breakers in 5 rows (4 keynoted GFCI correct, 22 provisional), 10 extra allowances, 31 RFIs, marked plans 3.3 MB with dots 3.8 to 6.6 ft off.

Text-layer facts on the ELEC set (pypdfium2, prototype): sheets E0.00 coversheet and legend (65 legend tags; contains a legend EXAMPLE scale "VIEW NAME 1/8" = 1'-0"" that must be ignored), E0.01 electrical specifications (the electrical specs live here; the project manual has no Division 26, 27 or 28), E0.02 single line (NO SCALE), E0.03 panel schedules (panels 1EELC, 1HD, 1LH, 1LI, all titled "(E) PANEL"; KEY column holds keynote 1 on exactly 4 rows, 1EELC circuits 1, 3, 5, 7: "FURNISH AND INSTALL NEW GFCI TYPE CIRCUIT BREAKER WITH ALL REQUIRED MOUNTING HARDWARE. BREAKER MANUFACTURER AND AIC RATING TO MATCH EXISTING."), E0.04 luminaire schedule (L1 to L5, X1; L4 control method "LOCAL"; L5 undercabinet linear), E1.00 overall plan at 1/16" = 1'-0" (16 ft per in; panel labels), ED2.01 demo power 1/4" (34 "(X)" tags in the plan area), ED3.01 demo lighting 1/4" (39 "(X)" tags), E2.01 new power 1/4" (74 circuit-tag tokens on 24 circuits; tags are stacked text labels, not one per device position), E3.01 new lighting 1/4" (44 tags on 3 circuits: 1HD-21 x33, 1LH-11 x10, 1HD-2 x1 "EXISTING LOAD"; `L5/1LH` x7, `L4/1LH` x3). Registration: E2.01 and E3.01 share 35 of 40 matched tokens at identical coordinates; ED2.01 matches E2.01 within 0.001; E2.01 shares NO tokens with E1.00, and ED2.01 shares only one two-word room label ("ADMIIN. ASSISTANT") with E1.00, so registration is single-anchor through the chain E2.01 -> ED2.01 -> E1.00 with k = 0.25 from the scale ratio. MECH sheets are M0.00 to M3.01 (all M-prefixed). SPECS: 34 sections, zero Division 26/27/28; relevant: 01 50 00 temporary facilities pp 24-30, 01 73 29 cutting and patching 34-36, 02 41 19 selective demolition 54-57, 07 84 13 firestopping 75-78; TOC says "DIVISION 28 - ELECTRONIC SAFTEY AND SECURITY - NOT USED". Exhibit A: "3.1.1 Nevada Revised Statutes ("NRS"), Chapter 338 - Public Works"; "Access controls and all Fire detection systems for the building are provided by Honeywell"; furniture connections by the general contractor; telemetry, Centrak and WAP by the successful bidder; "General contractors shall possess a valid B - General Building license".

Notes that decide scope on 7154: E0.01 general note 5 (EMT may be used in dry protected interior; rigid above ceiling; MC not allowed); E2.01 sheet note 3 (metal raceway in patient care); E2.01 sheet note 8 ("FOR CONDUCTORS WITHIN PVC FOLLOW NEC 352, AND EMT FOLLOW NEC 358", a code cross-reference, not a permission); E2.01 sheet note 5 (EC responsible for cutting, patching and painting; G3 excluded it); E2.01 sheet note 10 (hospital grade in patient care; tamper resistant in offices and corridors); ED3.01 sheet note 1 ("maintain and cap") vs keynote 1 ("removal ... to point of source"); E0.01 general note 4 ("provide new circuit breakers and/or fuses as required", generic, creates no rows); M0.01 ATC notes (controls contractor wiring).

### 3.3 Error catalog with root cause and fixing workstream

Dollar figures are bid basis (estimator 25% OH&P) unless marked cost.

| # | Error (7154 run) | $ impact | Root cause group | Fixed by | Acceptance |
|---|---|---|---|---|---|
| E1 | No pricing layer; quotes not linked; chat refused labor hours | whole bid absent | G5 | WS6 engine and chat tools; WS8 Pricing tab and sign-off; WS2 chat adapter | 3.5 row E1 |
| E2 | Zero branch wire and conduit: #12 6,718 FT, #10 12,295 FT, 3/4" EMT 6,338 FT (pre-waste) | $72,231 ($57,785 cost), 392 h | G1 | WS3 derive stage (route synthesis, always before main); WS4 gate `new_devices_no_branch`; WS7 band and rule R1 | 3.5 row E2 |
| E3 | Brief wrote a false rule ("no physical page calibration", "do not use per-device averages") | amplifier of E2 | G1 | WS3 (lengths exist before main; main prompt never asks the model for branch wire); WS4 `capability_denial` gate on all prose | 3.5 row E3 |
| E4 | Prevailing wage not concluded (NRS 338 in Exhibit A; project record says PW) | about $25k to $30k labor | G4 | WS5 bid conditions; WS6 rate by wage type | 3.5 row E4 |
| E5 | House-excluded general requirements carried (10 extra allowances) | $10k to $30k potential overbid | G4 | WS5 exclusions and allowance policy | 3.5 row E5 |
| E6 | Raceway left open (rigid above ceiling vs EMT) | $36,113 EMT line; rigid would add $40k to $50k | G4 | WS5 standards resolver (code references neutral; conflicts resolve to house value with `needs_review: raceway_conflict`) | 3.5 row E6 |
| E7 | Honeywell FA and access allowance kept although Exhibit A and "Div 28 NOT USED" exclude it; the specs sub had the fact, main dropped it | $5k to $20k potential | G4 (lost in consolidation) | WS5 verified facts; WS4 consolidator refuses and records `excluded_lines` | 3.5 row E7 |
| E8 | Exhibit A owner-to-GC obligations (telemetry, Centrak, WAP, furniture, door hardware, paint) treated as EC scope | $5k to $15k potential | G4 | WS5 party attribution | 3.5 row E8 |
| E9 | Vendor lighting quotes not linked; placeholder material 66% of quote; Codale repeats "16 L3" three times | $5,902 material | G5 | WS6 quote extraction and reconciliation | 3.5 row E9 |
| E10 | L5 undercabinet 7 EA instead of 43.46 FT (measured about 44.6; vendors quote 50 FT) | $4,145 | G1 | WS3 linear measure; WS4 `linear_in_ea`; WS6 `uom_conflict_quote` | 3.5 row E10 |
| E11 | Demo lighting 34 vs 49: troffers 5 vs 17 because a cropped escalation tile's subtotal became the sheet total | $1,910 | G3 | WS4 coverage, code regions, census floors; WS7 recount module (in reading); WS2 census and zoom | 3.5 row E11 |
| E12 | 22 provisional 20A/1P breakers on existing positions (only keynote 1, 4 GFCI, is new) | about $2,100 potential overbid | G4 | WS1 panel parser (status with positive evidence); WS5 breaker plan | 3.5 row E12 |
| E13 | Spec-grade device pricing (hospital grade duplex $32 vs library $12.20; GFCI $44.20 vs $22) | about $1,800 material | G5 | WS6 grade seeds driven by WS5 device facts | 3.5 row E13 |
| E14 | Thermostat relocations (5 EA) and #18/4C control cable (128.6 FT) dropped as mechanical ATC | $946 | G4, G1 | WS5 inclusion rules; WS3 control cable length | 3.5 row E14 |
| E15 | Demo track 3 EA instead of 50.07 FT | $676 | G1 | WS3 (keynote-anchored measure, else block finding to human; on 7154 only a measured track passes E15); WS4 gate | 3.5 row E15 |
| E16 | Legend never reached plan readers (derived page_meta role "other", ledger legend_pages 0) | review time | G2 | WS1 multi-label roles, legend text in the ledger; WS2 `legend_lookup` | 3.5 row E16 |
| E17 | Marked plans unusable: dots median 3.8 to 6.6 ft off, 116 of 208 over 5 ft, 23 stacked, no polylines | blocks verification of about $95k | G6 | WS8 code-built marks and polylines | 3.5 row E17 |
| E18 | Text layer never used (every sheet vector with full text) | enabler; $5 to $8 per run of vision tokens | G2 | WS1 page facts, facts block in sub requests; WS2 tools | 3.5 row E18 |
| E19 | L4 control "[unreadable]" (tile covered the L1 row; text says LOCAL); false RFI | 1 false RFI | G2, G3 | WS1 facts block; WS4 text-anchored regions; WS2 `zoom_region` with targets | 3.5 row E19 |
| E20 | Demo conflict ("maintain and cap" vs "remove to source") left as RFIs | demo $10,413 partial | G4 | WS5 standards (`demo_disposition`, keynote over sheet note) | 3.5 row E20 |
| E21 | TR receptacles not split; receptacles split by EMERGENCY or DEDICATED (G3 does not price these) | under $100 | G4 | WS5 device rules (merge unpriced modifiers; TR from legend symbol counts) | 3.5 row E21 |
| E22 | Over-split demo power rows plus an extra "make safe damper" row | about $180 | G3 | WS4 `propose_lines` demolition two-line style (flag value set by the WS4 golden style check, section 5.7); WS5 canonicalization | 3.5 row E22 |
| E23 | EMT accessories absent | $0 separately | G1 | WS3 emits `3/4"C EMT` in `_CONDUIT_RE` form; existing renderer derives accessories | 3.5 row E23 |
| E24 | MECH set read as electrical; all 170 spec pages sent | about $5 of $16.56 | G7 | WS1 category check and relabel, spec page selection, reduced reading of relabeled non-electrical drawings | 3.5 row E24 |
| E25 | Golden scorer mis-pairs rows and cannot sum split rows (reports recall 60.7%, MAPE 36%) | misleads every eval | G7 | WS0 scorer v2 | 3.5 row E25 |

Root-cause groups: G1 no measurement and no route synthesis (about $78,000 bid, 66%); G2 text layer and page roles unused; G3 counting completeness and partial evidence promoted to totals; G4 no G3 scope policy; G5 no pricing engine; G6 evidence a human cannot verify fast; G7 run hygiene and evaluation; G8 no tool-using agent.

Findings that are not AI errors: (1) G3 Verify leak: the General Material extraction took only WIRING material ($15,445.92); OTHER ITEMS material ($3,608.23: receptacles, connections, control cable) never reached Verify, so the sent bid is about $4,301 light. (2) The estimator omitted the 4 keynoted GFCI breakers ($533 bid), wrote a wrong L1 catalog number, and carried an L1-EM and L3-EM battery premium (about $751) that the legend and both quotes do not support. Ground truth needs adjudication.

The counterfactual that sets the build order: G3's own history priced on the estimator's quantities gives 637.1 h and a $118,105 bid (+0.3% vs the estimator's $117,758); G3's Verify formula on that basis gives $120,988 (+3.0% vs the $117,459 sent). Counting is mostly fine (17 of 22 count lines exact after summing split rows; 74 of 74 receptacles; schedules transcribed correctly; the AI found 4 GFCI breakers the estimator missed). Autonomy is blocked by measured quantities, scope policy and a pricing engine, in that order.

### 3.4 7154 ground truth (for acceptance tests)

Estimator BOQ, 28 non-accessory lines, quantities INCLUDE 10% wastage on conductor and conduit lines (base in brackets):

| Group | Line | Qty |
|---|---|---|
| DEMOLITION | REMOVAL OF LIGHTING FIXTURE AND CONTROLS W/ ASSOCIATE WIRE & CONDUIT BACK TO SOURCE | 49 EA |
| DEMOLITION | REMOVAL OF POWER DEVICES W/ BACKBOX & ASSOCIATE WIRE, CONDUIT BACK TO SOURCE | 35 EA |
| DEMOLITION | REMOVAL OF LIGHTING TRACK | 50.07 FT |
| WIRING | #12 THHN/THWN Cu | 7,390.15 FT [6,718.32] |
| WIRING | #10 THHN/THWN Cu | 13,524.28 FT [12,294.80] |
| WIRING | 3/4"C EMT | 6,971.48 FT [6,337.71] |
| OTHER ITEMS | DUPLEX RECEPTACLE, 125V | 54 EA |
| OTHER ITEMS | DUPLEX RECEPTACLE, 125V, GFCI | 11 EA |
| OTHER ITEMS | DUPLEX RECEPTACLE, 125V, TR | 3 EA |
| OTHER ITEMS | DUPLEX RECEPTACLE CONTROLLED BY OCCUPANCY | 4 EA |
| OTHER ITEMS | QUADPLEX RECEPTACLE | 1 EA |
| OTHER ITEMS | QUADPLEX RECEPTACLE CONTROLLED BY OCCUPANCY | 1 EA |
| OTHER ITEMS | ELECTRICAL CONECTION (sic) | 7 EA |
| OTHER ITEMS | #18/4C CONTROL CABLE | 141 FT [128.63] |
| OTHER ITEMS | RELOCATION OF THERMOSTAT TO NEW LOCATION | 5 EA |
| LIGHTING FIXTURES | L1 2'x4' recessed troffer | 16 EA |
| LIGHTING FIXTURES | L1-EM | 5 EA |
| LIGHTING FIXTURES | L2 2'x2' recessed troffer | 1 EA |
| LIGHTING FIXTURES | L3 6" recessed downlight | 13 EA |
| LIGHTING FIXTURES | L3-EM | 3 EA |
| LIGHTING FIXTURES | L4 task LED (control LOCAL) | 3 EA |
| LIGHTING FIXTURES | L5 undercabinet LED | 43.46 FT |
| LIGHTING FIXTURES | X1 LED exit sign | 2 EA |
| LIGHTING CONTROLS | DIMMER SWITCH | 10 EA |
| LIGHTING CONTROLS | DIMMER SWITCH W/ LOCKABLE COVER | 1 EA |
| LIGHTING CONTROLS | DIMMER & OCCUPANCY SENSOR SWITCH | 2 EA |
| LIGHTING CONTROLS | OCCUPANCY SENSOR | 3 EA |
| LIGHTING CONTROLS | OCCUPANCY SENSOR SWITCH | 1 EA |

Length lines (6): #12, #10, 3/4" EMT, #18/4C, L5, demo track. Count lines: 22. Accessory lines (6, from 6,337.71 FT EMT): 140 connectors, 698 couplings, 698 straps, 349 anchors, 349 screws, 70 boxes. Estimator marked plans legend totals: E2.01 `PW, 3#12, 3/4"C EMT 1,453.8 FT` and `PW, 3#10, 3/4"C EMT 3,994.3 FT`; E3.01 `PW, 3#12, 3/4"C EMT 890.7 FT` (sum 6,338.8 FT = the EMT row). Wire is 3.00 x conduit (one circuit per conduit, H+N+G).

Adjudications (file `EVAL_ROOT/references/7154/adjudications.json`, `reviewed: false` until Q1): ADJ1 add line "20A/1P CIRCUIT BREAKER, GFCI" 4 EA (keynote 1; estimator omitted; AI correct). ADJ2 L1-EM unit material 313 -> 209 and ADJ3 L3-EM 155 -> 78 (EM batteries not included per legend and both quotes). ADJ4 price truth: corrected G3 total. ADJ5 note: L5 measured 44.6 FT; truth stays 43.46. Case alias: `{"SHADED": "EM"}` (E0.00 legend: shaded luminaire = life safety branch).

Estimator workbook (Detailed Estimate): labor rate N4 $108, hours 588.008, cost $94,206.69, OH&P 25%/25%, TOTAL BID $117,758.36. Bid Recap sections: DEMOLITION $10,412.60, WIRING $72,231.29 (material $15,445.92), OTHER ITEMS $10,778.10 (material $3,608.23), LIGHTING FIXTURES $21,157.00, LIGHTING CONTROLS $3,179.38.

G3 sent (7 GCs, identical): labor 625 h x $125 = $78,125.00; materials (General Material manual quote $15,445.00 + Graybar lighting $17,550.00, both tax not included) taxed at 8.375% per category = $35,758.33; materials markup 10% = $3,575.83; bid $117,459.16. Codale quoted $17,362.00 (lower, not selected). Hour round-up 625 / 588.008 = 1.0629.

Corrected G3 total (adjudication ADJ4, computed with per-category cent rounding exactly as `verify_totals` must): General Material 15,445.00 + 3,608.23 = 19,053.23, taxed 20,648.94; Lighting 17,550.00 taxed 19,019.81; materials 39,668.75; markup 3,966.88; plus labor 78,125.00 = **$121,760.63**. (With the workbook's WIRING figure 15,445.92 instead of G3's rounded 15,445.00 the same formula gives $121,761.73; acceptance uses $121,760.63.)

Vendor quotes (text layer, lot priced, no unit prices): Codale CODALE26-82444-1 subtotal $17,362.00: 21 L1, 1 L2, 16 L3 printed three times, 3 L4, 50 L5 then continuation "50 FEET", 2 X1, 1 CONTROLS, 1 SERVICES; "SALES TAX IS NOT INCLUDED", "EM BATTERIES NOT INCLUDED. EM ON LIFE SAFETY CIRCUIT PER PLANS", "LIGHTING CONTROL SYSTEM INCLUDED". Graybar 676639-1126219-1 grand total $17,550.00: 21 L1, 1 L2, 16 L3, 3 L4 WAC BL-29903, 50 L5 (PER FOOT UNDERCAB), 2 X1, 1 CONTROLS LOT incl. 1/2 of commissioning; "SALES TAX NOT INCLUDED". EM folding: L1 16 + L1-EM 5 = 21, L3 13 + L3-EM 3 = 16.

Scorer v2 baseline on the stored v1 BOQ (E25 acceptance, raw truth): 17 of 22 count lines exact (L1, L1-EM, L2, L3, L3-EM, L4, X1; the five controls lines; duplex controlled 4, GFCI 11, quad 1, quad controlled 1; equipment connection 7); dollar coverage 0.319 +/- 0.005 ($30,021 of $94,207 covered); length lines produced 0 of 6; unit mismatches 2 (L5, demo track); duplex 57 vs 54; TR 0 vs 3; demo lighting 34 vs 49; demo power 34 vs 35; extras = 5 breaker rows (26 EA) and the make-safe damper row.

### 3.5 Acceptance checks: one pass criterion per check

This table is the only definition of each check's pass criterion; workstream acceptance paragraphs may add tests but never relax it. Detector: `plans_reader_acceptance.CHECKS["E<n>"]` (and `"GC<n>"`), each returning `pass | fail | skip | safe_refusal | error` with measured values. "Replay" = provable at $0 by replaying the stored 7154 sub outputs through the new code stages. "Baseline" = what the detector must measure on the stored weekend bundle; the self-test asserts FAIL with these values. "7154 smoke" = whether the row gates the 7154 smoke case of 1.3 item 1 and when `skip` is allowed. Thresholds use the metrics of 3.6.

| Check | Pass criterion (exact threshold) | Reads (stage) | Replay | Baseline (weekend bundle) | 7154 smoke |
|---|---|---|---|---|---|
| E1 | Priced artifact `bdr.priced_estimate/1` exists for the run's latest BOQ version, `refused` false, >= 98% of non-allowance lines `priced` (unit_hours and unit_material_usd not null), `bid_total_usd == sum(verify)` to the cent; if a chat probe ran, a pricing-role "how many labor hours" answer is a number within 15% of `totals.hours` | priced_estimate (price), chat_probe | yes (chat part needs cassette or live) | FAIL: no priced estimate; chat declined hours | gates; chat part may `skip` only when no chat probe ran |
| E2 | New devices > 0 implies branch conductor FT > 0 and branch conduit FT > 0 (all cases). 7154: #12 vs 7,390.15, #10 vs 13,524.28, 3/4" EMT vs 6,971.48 FT (wastage-inclusive) each `acceptable`; EMT `good` | BOQ (derive, consolidate) | yes | FAIL: 0 / 0 / 0 FT | gates |
| E3 | On the run itself: zero hits in main output prose and the rendered brief text for `physical page calibration`, `do not use per-device averages`, `(no\|without) .*invented assumed routes`, `printed scale alone .* insufficient`, `cannot be defensibly measured`; every FT line has a non-null basis | main, brief (consolidate, render) | no (main is LLM); the $0 part is `test_gates_legacy_7154` raising `capability_denial` | FAIL: 4 hits | gates |
| E4 | Priced `wage_type == prevailing_wage`, labor rate $125.00, no RFI or question matching `prevailing wage\|NRS\s*338\|Davis.?Bacon` | priced_estimate, main (derive, price) | yes | FAIL: no wage decision | gates |
| E5 | No priced line or allowance with amount > 0 matching `TEMP(ORARY)? (POWER\|DISTRIBUTION\|LIGHTING)\|HEPA\|COORDINATION STUDY\|COMMISSIONING\|PATCH\|FIRESTOP\|\bBIM\b\|TRAINING\|PHASING\|ANCILLARY`; each detected item is in exclusions or clarifications; extra allowances 0 | priced_estimate, estimate spec (enforce) | yes | FAIL: 8 house-excluded of 10 extra allowances | gates |
| E6 | Branch conduit type EMT; EMT FT >= 0.75 x truth; RGS, RMC, IMC branch FT 0; a clarification names EMT; no raceway RFI | BOQ, priced_estimate (derive, enforce) | yes | FAIL: no conduit | gates |
| E7 | No priced fire alarm or access-control line or allowance; exclusions name fire alarm and access control by others (Honeywell) with a citation to an Exhibit A page | priced_estimate (enforce) | yes | FAIL: Honeywell FA and access allowance carried | gates |
| E8 | No priced row matching `TELEMETRY\|CENTRAK\|\bWAP\b\|WIRELESS ACCESS\|FURNITURE (?!CONNECTION)\|DOOR HARDWARE\|\bPAINT`; telemetry, Centrak, WAP, furniture, door hardware and paint each appear in exclusions or clarifications with a citation | priced_estimate (enforce) | yes | FAIL: "TELEMETRY / CENTRAK / WAP SURVEY AND REINSTALLATION" | gates |
| E9 | LIGHTING FIXTURES `material_source == vendor_quote`, pre-tax within 2% of $17,362.00 (Codale, lowest eligible, Q9) or of $17,550.00 when the Graybar selection is supplied, with a recorded reason; quote reconciles: L3 once (16, collapse warning), L5 50 FT, L1 21 and L3 16 after EM folding | priced_estimate (price) | yes (quote PDFs parsed by code) | FAIL: no quotes | gates |
| E10 | L5 unit FT and `acceptable` vs 43.46 FT (target `good`); the stored v1 BOQ (L5 7 EA) raises `uom_conflict_quote` | BOQ (derive), pricing findings | yes | FAIL: 7 EA | gates |
| E11 | Demolition lighting within 10% of 49; ED3.01 troffers 17; no BOQ row whose basis is a tile subtotal; text-tag census vs BOQ mismatch <= 10% or a blocking gate | BOQ, gate report (recount, merge) | partial: coverage and census gates at $0; the count needs the live recount | FAIL: 34 (37 with track EA rows) | gates (live) |
| E12 | Breaker lines = exactly one line "20A/1P CIRCUIT BREAKER, GFCI" 4 EA (ADJ1); zero provisional breakers on SPARE or EXISTING positions | BOQ (enforce) | yes | FAIL: 26 EA in 5 rows, 22 provisional | gates |
| E13 | Duplex unit material >= $22.40 (0.7 x $32.00) and GFCI >= $30.94 (0.7 x $44.20), each with a cited hospital-grade spec fact | priced_estimate (price) | yes | FAIL: no pricing | gates |
| E14 | Thermostat relocation 5 EA exact; #18/4C control cable `acceptable` vs 141 FT | BOQ (derive) | yes | FAIL: both absent | gates |
| E15 | Demolition track is a FT line with basis `measured`, `acceptable` vs 50.07 FT (target within 10%). A human-routed `linear_in_ea` block finding instead of a length is `safe_refusal` on other cases and FAIL on 7154 | BOQ, gate report (derive) | yes | FAIL: 3 EA | gates; no skip |
| E16 | Ledger `legend_pages` contains E0.00; ledger legend has >= 60 entries; with facts flags on, every planned drawing-category sub request contains the ledger legend text (T-INT-2); fixture rows with a NEED TO CONFIRM caveat 0 | ledger, planned requests (plan); BOQ caveats (live) | yes for ledger and requests; caveat part live | FAIL: `legend_pages: []`, page_role `other` everywhere | gates; caveat part may `skip` on replay |
| E17 | Marks: median distance to the nearest text-layer anchor of the mark's tag <= 1.5 ft (sheet scale); <= 10% of marks over 5 ft; stacked duplicates (two marks within 0.25 ft) <= 2; every FT line with geometry has a polyline | marked plans spec, evidence (evidence) | yes | FAIL: median 3.8 to 6.6 ft, 116 of 208 over 5 ft, 23 stacked, no polylines | gates |
| E18 | >= 90% of drawing pages carry a sheet number, a title and a page_role other than `other` from the text layer; every document `facts_status = done`; with facts flags on, every planned sub request carries a facts block for each page it sends (T-INT-2); planned reading input for 7154 <= 940k tokens (the stored run's 940k) | documents, ledger, planned requests (plan) | yes | FAIL: titles None, role `other` on all drawing pages, no facts block | gates |
| E19 | No output text contains "unreadable" for a value present in the text layer; L4 control = LOCAL; no question about the L4 control method | category outputs, main (reading) | no (sub reading is live); $0 part: zoom retarget test (WS2) | FAIL: "L4 control remains unreadable", 1 false RFI | gates (live) |
| E20 | No RFI or question on "maintain and cap" vs "remove ... to source"; demolition lines carry a `demo_disposition` decision with a citation | main, scope report (enforce) | yes | FAIL: left as RFIs | gates |
| E21 | TR duplex line 3 EA exact (from REC-TAMP legend symbol counts); zero receptacle rows split only by unpriced modifiers (EMERGENCY, ESSENTIAL, NORMAL, LIFE SAFETY, DEDICATED, colors); receptacle total 74 | BOQ (enforce) | yes | FAIL: TR 0; 4 style-split rows | gates |
| E22 | Demolition power: one row when `plans_reader_demo_two_line_style` is true, at most 2 rows when false; their sum within 1 of 35 (exactness of 35 is scored by the count-line metric, not here); no "MAKE SAFE" damper row | BOQ (consolidate, enforce) | yes | FAIL: 4 rows summing to 34 plus the damper row | gates |
| E23 | When EMT FT > 0 the rendered BOQ has EMT connectors, couplings and straps; couplings within 10% of 698 | rendered BOQ (render) | yes | FAIL: no EMT, no accessories | gates |
| E24 | No document whose numbered sheets are >= 80% M-prefixed is read as electrical_drawings (relabeled, reduced reading); spec pages sent to models <= 40% of spec pages (7154 <= 68 of 170) | documents, groups (plan) | yes | FAIL: MECH read as electrical; 170 spec pages sent | gates |
| E25 | Scorer v2 on the weekend bundle: 17 of 22 count lines exact raw, dollar coverage 0.319 +/- 0.005, L1 <-> L1 and L1-EM <-> AI "LIFE SAFETY" (5), TR never paired with a demolition row. On a new run: scored with MATCHER_VERSION 2 and zero unresolved truth-key collisions | scorer (score) | yes | FAIL under the v1 scorer (recall 60.7%, MAPE 36%, L1 mis-paired) | gates |
| GC1 | Every non-quoted material section carried: for each pricing category without an eligible quote, priced material > 0 when the truth recap has material there (7154: OTHER ITEMS material > 0) | priced_estimate (price) | yes | FAIL: no pricing | gates |
| GC2 | Generic scope score (3.6): topic recall >= 0.7 and 0 priced lines on a topic the sent proposal excludes | main, priced_estimate, sent proposal text | yes (scoring) | not run (no scope report) | report only on 7154; gates nothing per case; core and holdout report the median |
| GC3 | Every refusal maps to a confirmed defect by the 3.6 mapping; otherwise `false_refusal` | gate report, reference | yes | not applicable | 7154 must not refuse (1.3) |
| GC4 | `excluded_project_ids_sha256` equals sha256 of the sorted excluded ids and they include the case's own project; library, mining, missed list and bands built with the exclusion | priced_estimate metadata | yes | FAIL: field absent | gates |

### 3.6 Metric definitions

- Count line exact: scorer v2 match with `|ai - truth| <= 0.02 x truth` (`EXACT_TOLERANCE`, relative), so 35 means exactly 35.
- Length bands, on wastage-inclusive quantities: `rel = |ai - truth| / truth`; `good` <= 0.10, `acceptable` <= 0.25. A length truth line with no AI line, or with an EA line, scores `none`.
- Line value `v` (estimator cost basis): truth qty x (estimator unit material + estimator unit hours x the workbook labor rate), from the estimator workbook of the case (7154: rate $108, sum of v = $94,207). Accessory lines excluded.
- Dollar coverage = sum of v over truth lines that are matched and in band (count exact, or length `acceptable`) / sum of v.
- Dollar-weighted error = sum over truth lines of `min(1, |ai_qty - truth_qty| / truth_qty) x v` / sum of v; an unmatched truth line contributes `1 x v`. AI lines with no truth match are reported separately as `extras_value` (priced at library medians) and do not enter this metric.
- Bid error = (AI `bid_total_usd` - G3 sent total) / G3 sent total, sent total per 8.6. Refused cases count as misses.
- Noise floor per count kind k (scorer v2 `classify_line` kind): `nf_rel_k = |qA_k - qB_k| / truth_k(7154)` where A is the final 7154 integration run and B the 7154 noise repeat on the same code; kinds absent on 7154 use 0.05. A case regresses on k when `|new_k - truth_k| - |old_k - truth_k| > max(1, nf_rel_k x truth_k)` with `old` the case's old-pipeline bundle (7154 weekend bundle, the 7172 and 7177 dev runs); cases without an old-pipeline bundle `skip` this check. Two runs give a coarse floor; the report states n = 2.
- GC2 generic scope score: topics are the WS5 seed lexicon (temporary power, temporary lighting, HEPA, coordination study, commissioning, cutting and patching, painting, firestopping, BIM, training, phasing, bond, fire alarm by others, low voltage by others, permits and fees, after-hours work), each a word-bounded regex. S = topics hit in the latest sent proposal docx text; A = topics hit in the AI exclusions, clarifications and suggested proposal lines. Recall = `|A intersect S| / |S|` (skip when S is empty). Violation = an AI priced line with amount > 0 whose description hits a topic in S. Pass per case: recall >= 0.7 and 0 violations.
- GC3 refusal match, gate code -> what truth must confirm: `census_floor`, `partial_basis` -> truth count for that family on that sheet > the AI count; `new_devices_no_branch` -> truth branch wire > 0 and AI 0; `linear_in_ea` -> the truth line for the item is FT; `excluded_system`, `fact_conflict` -> truth omits or excludes the system; `uom_conflict_quote` -> the truth unit equals the quote unit and differs from the AI unit; `wage_type_unknown` -> `projects.wage_type` is null; every other gate (`unsourced_line`, `orphan_rows`, `arithmetic`, `brief_numbers`, `capability_denial`, `lost_sheets`, `double_consumption`, `unverifiable_citation`, `unit_class`) -> a cited line differs from truth by more than 10% or is absent in truth. No confirmation: `false_refusal`.
- False-confident rate (WS6 LOPO): among lines priced with quality `strong` or `good`, the share whose LOPO unit labor hours or unit material differ from the case's own estimator unit value by more than 35%. Gate <= 5%.
- Scorer `other` share (WS0 classifier gate): share of non-accessory truth lines, and of their v, whose `classify_line` kind is `other`. Gate <= 15% of lines and <= 10% of v.

---
## 4. Target architecture

### 4.1 The pipeline end to end (one canonical stage spine)

Run statuses do not change (no new run status). New work happens as new group kinds inside the existing statuses, plus a post-run chain on a `done` run that uses the same change slot as modify.

```
create_run (eval_case_id optional; routes and conventions snapshotted)
  |
splitting ......... splitter (unchanged)
  |                 + page facts job per document (WS1, job type plans_reader_facts, non-LLM, cached by sha256)
  |                   job end calls plans_reader_runs.advance(run) for every run waiting on that file
  v
_plan_reading ..... page_meta from facts (sheet numbers, multi-label roles), category check and relabel,
  |                 spec page selection, ledger with legend text, panels, luminaires, scales (WS1)
reading
  |  pass 1: sub groups (single call, no tools in this build) + per-page facts block (WS1)
  |  pass 2: escalation groups, regions computed by code from text anchors and tag clusters,
  |          containment recorded, patches carry supersedes_row_id and coverage (WS4)
  |  per category when its groups settle:
  |     code merge (row ids, coverage, alternates; WS4) -> census floors (WS4 on WS1 tags)
  |     -> pre-gates: census_floor, partial_basis -> recount targets (+ top-dollar count rows)
  |  pass 3: recount groups (WS7 recount module, small-context tool loop, provider by A/B)
  |          marks deduped by tile ownership -> COUNT rows with supersedes_row_id -> re-merge once
  v
consolidating
  |  derive group (kind derive, code, $0 unless assists):
  |     1. scope_engine.prepare (WS5): bid conditions, facts, standards, breaker plan,
  |        inclusions, device facts -> outputs scope 'policy' (optional capped attribution call)
  |     2. build_measure (WS3): scales per view, registration, panel locations, circuits,
  |        route synthesis, linear items, control cable, quantity rows M1..Mn -> outputs scope 'measure'
  |  main group (kind main):
  |     mode tools: WS4 consolidator tool loop; code computes every quantity from cited rows
  |                 (C, R, S, M ids); scope digest and facts are binding; finish() runs final gates
  |     mode legacy: today's single call (fallback), then apply_measure_to_boq + enforce
  v
rendering (_finish_with_main, fixed order, all code)
  |  1 assemble main output (WS4) or legacy path
  |  2 scope_engine.enforce (WS5)  [idempotent; records excluded_lines]
  |  3 final gates (WS4) -> plans_reader_gate_findings
  |  4 evidence.build (WS8) -> outputs scope 'evidence'
  |  5 render deliverables (BOQ, estimate, brief, markups, marked plans from evidence,
  |    annotated plans) -> write main output -> run done
  |  6 enqueue ai_pricing job (WS6) when AI_PRICING_ENABLED and AI_PRICING_AUTORUN
  v
done  (quantities complete; autonomy_state = in_progress while the post-run chain runs)
  |
post-run chain (each step takes the run's change slot like modify; never changes run status)
  |  ai_pricing job (WS6; code, plus the optional quote fallback LLM call): price the current
  |     BOQ version with library, policy, quotes, wage type and gate verdict
  |     -> plans_reader_pricing version v, priced workbook; pricing-stage findings
  |     (uom_conflict_quote) written through WS4 persistence with stage 'pricing'
  |  review (WS7): deterministic lane (bands, rules, Verify invariants) -> review group
  |     (fresh-context reviewer, read-only tools) -> revise group (one bounded turn, typed ops)
  |     -> apply mode: ops go through the modify path (BOQ deliverable v+1, rendered)
  |        -> re-price (pricing v+1) -> recheck lane ($0)
  |  autonomy.recompute (WS8): plans_reader_runs.autonomy_state
  v
human verifier: Evidence, Checks, Pricing tabs -> sign-off (verified or rejected)
```

Why this order: quantities must be right before measurement and main; measurement and scope standards must exist before main so the brief can never deny measurement (E3) and main never types a length; pricing reads a BOQ that already passed the gates; review is a check on a priced estimate and feeds back through the same modify path humans use, so the files the verifier opens always match the priced version.

Findings by stage. Final gates (render step 3) are `stage = final` and are the only input to `gates.pricing_allowed(run)`. `uom_conflict_quote` can only be known after quotes are reconciled, so WS6 raises it inside the ai_pricing job (after render step 6) and records it with `plans_reader_gates.record_findings(sb, run, findings, stage="pricing")` (WS4 owns persistence and the `stage` CHECK includes `pricing`). A pricing-stage block sets that line's price status `blocked` and the priced version's status `blocked`, never re-renders the BOQ, and is read by `autonomy.recompute` and by the next pricing version (a modify that fixes the unit supersedes it). Review findings are WS7's own table.

The ai_pricing job is an LLM-capable job type: it is claimed in the plans claim pass (not in `NON_LLM_JOB_TYPES`), and when `quote_lines_llm_fallback_enabled` is on its fallback call goes through `llm.py` as feature `pricing_quote` with the ledger guard and lease renewal. With the fallback off it makes no model call.

### 4.2 Group kinds and where they run

| Kind | Runs in run status | LLM | In PIPELINE_KINDS | Owner |
|---|---|---|---|---|
| sub, escalation | reading (and consolidating, as today) | yes | yes | existing, WS4 changes escalation |
| rollup | reading | yes | yes | kept only behind `plans_reader_llm_rollup` (default false) |
| recount | reading | yes (tool loop) | yes | WS7 module, WS2 runtime |
| derive | consolidating | no (optional capped assists) | yes | WS3 and WS5 |
| main | consolidating | yes | yes | WS4 |
| review, revise | done, done_with_errors (post-run, change slot held) | yes | no | WS7 |
| modify, chat | as today | yes | no | existing |

LLM feature keys (registered in `llm._FEATURES`, labeled by key and provider in the AI Monitor and spend logs): existing `plans_sub` (sub, rollup, escalation), `plans_main` (main, legacy and consolidator), `plans_modify`, `plans_chat`; new `plans_recount`, `plans_review`, `plans_revise`, `plans_derive` (measure assist), `plans_scope_attrib` (WS5 attribution fallback), `pricing_quote` (WS6 quote fallback). `FEATURE_BY_KIND` gains recount, review, revise and derive. Today `_plans_feature()` sets `openai_only=True`; WS2 replaces that pin for plans features with route resolution: `complete_turn` takes provider, model and effort from the run's snapshotted route for the step (`plans_reader_routes`), falling back to `openai_plans_model` when the step has no route; the full-self-hosted exemption covers whichever provider the route names when Q22 allows it (dev yes, prod at release). `pricing_quote` and `plans_scope_attrib` use the same `_plans_feature` switch (`plans_reader_enabled`). Non-plans features are unchanged.

`_ACTIVE_RUN` becomes a per-kind map (`plans_reader_agents.py:1788`). `run_group` dispatches non-LLM kinds (derive) before the cap and builder checks, so a $0 group can never be paused `over_cap`. Review kinds over a cap or budget end the review `partial` (never queued forever). `groups_total` counts PIPELINE_KINDS only.

### 4.3 The tool loop (WS2)

One llm_jobs job per group, as today. Inside it, `plans_reader_toolloop.run_loop(ctx)` runs N rounds, each a durable `plans_reader_loop_rounds` row, with tool results stored in `plans_reader_tool_calls` (idempotent by call id), lease renewal before every call and tool, and a per-group dollar budget enforced through `ai_spend.gate_round` (group budget, run ceiling, daily cap, eval ledger).

Two rules make autonomy independent of the model's choices:
1. Deterministic pre-call stage: before round 1 the kind's builder runs tools in code and injects the cited results into the first user turn (recount: census of the target family on the sheet, the code region, legend lookup of the symbol; main: get_census, propose_lines candidates, measure quantities, scope digest; review: bands, rule findings, census table). The model starts with the evidence in its ledger.
2. Tools that need a target refuse without one: `zoom_region` with purpose `read_text` or `count` on a page with a text layer requires an anchor, `expect_text` or `census_ref`; without one it returns candidates at $0 and no image.

Forced `tool_choice` is never used (it returns 400 on Claude Opus 5.5 and Sonnet 5.5). The final round steers with the text "Tool use is finished. Answer now in the required format with what you have." On OpenAI tools are omitted on the final round; on Anthropic `tool_choice` stays `auto` (changing it would drop the messages cache) and a stray tool call is answered with an error result and one bounded extra round.

### 4.4 Gates, pricing, review, evidence in one picture

- Integrity gates (WS4) are code. Blocking findings never lower a number. Final-gate routes are `model_fix` (inside the consolidator loop only) or `human`; recount and measurement already happened before main.
- Pricing (WS6) is code: library lookup with strict UoM, quote reconciliation, the Verify formula. It reads `gates.pricing_allowed(run)`; a blocked run still gets a priced draft labeled BLOCKED (status `blocked`) so the verifier sees the number and the reasons.
- Review (WS7) is a check after pricing: bands and rules are code; one reviewer model; one revise turn; code applies ops only with evidence; decreases need two signals.
- Evidence (WS8) is code: marks at text-layer anchors, polylines from WS3 routes, chips per line, autonomy status combining every check.

---

## 5. Canonical contracts

One contract per thing. Where the workstream designs disagreed, this section wins.

### 5.1 Conventions

- Coordinates: page fractions of the DISPLAYED page (after `/Rotate`, inside the CropBox), origin top-left, x right, y down, 4 decimals. This is the frame of `plans_reader_images.box_from_fractions`, `render_page_png` crops and escalation tiles. WS1 transforms pdfium user space into it (`page_frame`: `u = (x - l) / (r - l)`, `v = (t - y) / (t - b)`, then rotation 90: `(1 - v, u)`, 180: `(1 - u, 1 - v)`, 270: `(v, 1 - u)`). Sheet inches = fraction x `w_in` or `h_in`. A rotated-page fixture test is required in WS1, WS2 and WS8.
- Wage type enum everywhere: `prevailing_wage | non_prevailing_wage` (the `projects.wage_type` values). Unknown is `null` plus `needs_review: wage_unknown`.
- Row ids (WS4): run-unique, never reused: `C<n>` counts, `R<n>` runs, `S<n>` schedules, `P<n>` specs, `O<n>` scope, `D<n>` details, `Q<n>` questions, `M<n>` measure quantity rows (WS3), `F<n>` facts (WS5), `B<n>` BOQ lines, `K<n>` transient consolidator candidates. Priced lines, review ops, evidence marks and the WS0 artifact all key on the BOQ `line_id` (`B<n>`). Legacy specs without ids get transient ids in BOQ order through `rows.iter_rows`.
- Quantity basis enum (BOQ line v2): `counted | schedule | measured | assumed_route | stated | census_floor | policy | mixed | manual`. Measure-internal basis (WS3): `measured_route | assumed_route | assumed_route_untagged | assumed_allowance | assumed_density`, mapped to the line basis `measured` (first) or `assumed_route` (rest) with caveat `(ASSUMED)`.
- Price quality enum everywhere: `strong | good | weak | policy | quote | missing`.
- Text counts are floors. "None found" means unknown, never 0. Pages without a usable text layer report `text_layer_quality: none|sparse|vector_text`, and census returns `method: "unavailable"`, never a count of 0.
- Item identity lives in ONE module, `app/services/electrical_items.py` (WS6 creates it at S3 by moving verbatim from `scripts/plans_reader_golden.py`: everything at lines 138 to 356 (`_FRACTIONS`, `_QUOTES`, `_DASHES`, `_clean_text`, `normalize_unit`, `_strip_caveats`, `normalize_description`, `_normalize`, `_heading_name`, `_canonical`, `_KEY_PATTERNS`, `_TAG_RE`, `_POLE_WORDS`, `_MODIFIERS`, `_CONFLICT_PENALTY`, `_VOLTAGE_RE`, `key_tokens`, `_SYNONYMS`, `_ratio_text`, `similarity`) AND the module-level dependencies above them: the `BOQ_GROUPS, BOQ_LEVEL0, BOQ_SUBGROUPS, UNITS` import from `plans_reader_schemas`, `_KNOWN_SUBGROUPS`, `_SUBGROUP_PARENT`, `_ACCESSORY_WORDS`, `_CAVEAT_WORDS`, `_UNIT_ALIASES`, `MATCH_THRESHOLD`, `EXACT_TOLERANCE`, `WASTAGE_FACTOR` and `class BoqRow`; then it adds `parse_item`, `ItemIdentity`, `hard_conflict`, `canon_uom`, `convert_unit_value`, `count_key`, `item_class`, `LINEAR_CLASSES`). `electrical_items` imports nothing from `scripts/`, so there is no cycle. The golden script, the WS0 scorer and WS4 import it. The script re-exports every moved name (`from app.services.electrical_items import *` plus the underscore names explicitly) so its 25 tests, which use `golden.BoqRow` and `golden.similarity`, pass unchanged.
- `LINEAR_CLASSES` (always FT, an EA row blocks): conductor, conduit, cable, raceway, trench, sawcut, lighting_track (track without "head"). Linear luminaires (undercabinet, strip, tape, linear, cove) are FT only on evidence: the fixture schedule says per foot or LF or "lengths as required", the vendor quote prices per foot or has a FEET continuation, or WS3 measured it. Otherwise EA is allowed with a warning (G3 carries LINEAR EA 47 vs FT 21 and STRIP EA 36 vs FT 9 in the workbooks).

### 5.2 Tool catalog

Every tool is registered once in `app/services/plans_reader_tools.REGISTRY` as a WS2 `ToolSpec(name, description, args_schema, result_doc, mode, kinds, cost_class, internal_pricing, needs_pro_max, max_calls_per_group, timeout_s, handler(ToolContext, dict) -> ToolResult)`. `mode` is read, review or write. `cost_class` is `free` or `image` (a tool that calls a model is rejected). Args are strict JSON Schema (every property required, nullable via `["type","null"]`, `additionalProperties: false`), validated server-side with `jsonschema` (add `jsonschema` to `pyproject.toml` and `uv.lock`). Results are compacted (12,000 chars per result, 40,000 for get_output and get_deliverable, 80,000 per round), carry `citations`, and print document text as data. Offer order is sorted by name (cache stability). Every tool definition is registered with the prompt hash (`plans_reader_prompt_hash.register_prompt_part`).

Kinds below: RC recount, M main (consolidator), RV review, RS revise, D derive assist, C chat (pricing tools only for pricing roles); "all" means these six. Sub (S) and escalation (E) get NO tools in this build: they stay single calls (WS2, section 8.3), and WS1 facts reach them only as the facts block and the ledger. There is no sub-tools flag; adding tools to S or E is a later build. `needs_pro_max` means the tool is offered in chat only when the user turned on Pro Max for that message (the existing chat toggle stored as `plans_reader_chat_messages.pro_max`, PLANS_READER.md 3.9, which lets chat look at rendered pages); it has no effect for other kinds.

| Tool | Args (abridged) | Returns (abridged) | Implemented by | Mode, kinds |
|---|---|---|---|---|
| `sheet_index` | {} | sheets: sheet number, doc_ref "D1 P9", title, discipline, roles, views with scale, w_in, h_in, text_layer_quality | WS1 | read; all |
| `text_search` | query (1..120), document_id or null, pages or null, sheets or null, mode exact or token or prefix, limit or null (max 100), offset or null | hits {ref t:, document_id, page, sheet, text, bbox, region}, total, omitted, next_offset (no regex from models) | WS1 TextIndex | read; all |
| `zoom_region` | document_id, page, bbox or null, anchor {ref, text, occurrence} or null, expect_text or null, census_ref or null, purpose read_text or count or verify_symbol or measure, ppi or null | views {view_id, bbox}, bbox_used, ppi, retargeted, retarget_reason, coverage {targets, inside, complete}, text_lines, candidates, warnings, text_layer_quality + images | WS2 (algorithm section 6 WS2) | read, image; RC, RV, C (Pro Max) |
| `census` | document_id, page, pattern {kind tag or symbol or text, value}, region or null | census_id, ref c:, count or null, method text_layer or vector_symbol or unavailable, occurrences, clusters, family_specific bool, note | WS1 (text tags); symbol kind needs a legend ref `l:...` and returns `unavailable` until a vector matcher exists | read; RC, M, RV, C |
| `get_schedule` | document_id or null, kind panel or luminaire or equipment or feeder or any, name or null, row_key or null | schedules {schedule_id run-unique "doc8:sN", kind, name, page, bbox, columns, rows {ref s:, key, cells, parsed}} | WS1 | read; all |
| `legend_lookup` | query or null, effect or null | entries {ref l:, tag, description, device_type, modifiers}, modifiers | WS1 | read; all |
| `keynote_lookup` | sheet, number or null | items {number, text, bbox}, markers | WS1 | read; RC, M, RV, C |
| `spec_lookup` | section or null, keyword or null, limit | hits {ref sp:, section, title, page, text}, sent pages or snippets or title only | WS1 | read; M, RV, C |
| `measure_polyline` | document_id, page, points [[x,y]], vertical_ft or null | measure_id, ref m:, length_ft or null, scale {text, ft_per_in, view_id, source}, flags | WS3 MeasureService | read; RC, M, RV, RS, D, C |
| `synthesize_routes` | document_id or null, page or null, circuits or null | routes {route_id, ref r:, circuit, panel, length_ft, home_run_ft, basis, line_ids}, totals by size, caveats (reads the stored measure output) | WS3 | read; M, RV, RS, C |
| `find_linear_items` | document_id, page, tag | candidates {id, bbox, length_ft}, attaches a numbered overlay image | WS3 | read, image; D, RV, C (Pro Max) |
| `explain_quantity` | row_id (M id) | derivation terms per circuit and segment, scales, registrations | WS3 | read; M, RV, RS, C |
| `set_linear_item`, `set_panel_location` | section 5.8 | new measure output version with an overrides entry (human beats model) | WS3 | write; D, C (writer role and change words) |
| `get_scope_policy` | topic or null, item_description or null | rules {ref p:, rule_id, topic, decision include or exclude or allowance or by_others, default_text, proposal_line}, bid_conditions {wage_type, basis_refs, needs_review}, standards, decisions | WS5 | read; M, RV, RS, C |
| `get_rows` | kind C or R or S or P or O or D or Q or M or null, sheet_key or null, query or null, status or null, offset or null | compact rows {id, sheet_key, description, tag, qty, unit, basis, coverage, conf, status, consumed_by} | WS4 | read; M, RV, RS, C |
| `get_row` | id | full row with positions, superseded, alternates, origin | WS4 | read; M, RV, RS, C |
| `get_census` | sheet_key or null | floors {family, floor, accounted, clusters} | WS4 | read; M, RV |
| `get_facts` | system or null | facts F* with verified flag and cite | WS5 (handler and registration in WS5's `scope_tools.py`) | read; M, RV, C |
| `propose_lines` | section or null | candidates K* with refs, computed quantity, suggested BOQ position | WS4 | read; M |
| consolidator writes: `add_boq_line`, `accept_candidates`, `update_boq_line` (fields as nullable properties), `remove_boq_line`, `account_rows`, `add_allowance`, `add_exclusion`, `add_assumption`, `add_rfi`, `add_risk`, `write_brief`, `set_estimate_notes`, `set_material_notes`, `finish` | section 5.8 | section 5.8 | WS4 | write; M only |
| `price_lookup` | description, uom enum (EA, FT, LF, C, M, CU FT, CY, SQ FT, LS), quantity or null, family_hint or null, spec_facts [{key, value}] | status ok or no_match or uom_conflict or ls_not_priced, matched, labor_hours {median, p25, p75, n_obs, n_projects, quality}, material {..., latest_seen}, conversion, modifiers_applied, extended, candidates (never project or GC names) | WS6 | read, internal_pricing; M, RV, RS, C |
| `get_price_summary` | {} | latest pricing version: status, Verify draft, hours basis and reviewer hours, rate, range, quality shares, blockers, top lines | WS6 | read, internal_pricing; RV, C |
| `explain_row` | line (id "B12" or text), include_marks | derivation text, sources with coverage, marks by method, polylines_ft, gate and review findings, price block only for pricing actors | WS8 | read; RV, C |
| `record_finding` | severity, line_ids, claim, evidence (ledger refs), suggested_fix | finding_id, ref f: | WS7 | review; RV |
| chat legacy: `get_brief`, `list_documents`, `get_output` (paged), `search_outputs`, `get_deliverable`, `read_pages` (Pro Max), `modify_deliverable` (write, existing four gates) | unchanged | unchanged | existing | C |

Dropped from the designs: WS2 `write_rows` (main writes only through the WS4 tools; the main final output is assembled by code), WS1 standalone `run_tool` dispatcher and `schedule_lookup` and `page_facts` names, WS3 `get_scales`, `locate_panel`, `register_sheets`, `get_branch_quantities` as model tools (code APIs and derive-assist internals only), WS5's five extra tools (folded into `get_scope_policy`), WS6 `explain_price_line` and WS8 `get_estimate_totals` (use `explain_row` and `get_price_summary`).

Citation refs (opaque to models, stable within a run, resolvable by the issuer): `t:{doc8}:{page}:{run}` text run, `c:{census_id}[:k]` census, `s:{schedule_id}:{row}`, `l:{legend_id}:{entry}`, `sp:{doc8}:{page}:{section}` (WS1); `v:{view_id}` delivered crop (WS2); `m:{measure_id}`, `r:{route_id}` (WS3); `p:{rule_id}` (WS5); `$:{item_key}@{library_version}` (WS6); `f:{finding_id}` (WS7); row ids C, R, S, M, F (WS4, WS3, WS5). `doc8` = first 8 hex chars of the document uuid (collision falls back to the full uuid).

Citation validation (`plans_reader_tools.validate_rows`, used by recount output and consolidator writes): every cited ref is in the group's ledger or resolvable; basis vs ref kind (counted needs v:, c: or t:; schedule needs s:; measured needs m: or a measured r:; assumed_route needs r:; policy needs p:); a count supported only by incomplete coverage cannot be a sheet total; census floor applies to the SUM of rows on the same sheet that cite the same census id, not per row, and only when the census is family-specific (narrowed by legend class or region); linear classes must be FT.

### 5.3 Service protocols (WS2 defines; providers register at import with `register_services`)

`TextIndex` (WS1: `runs(document_id, page)`, `search(query, document_ids, pages, mode, limit, offset)`), `CensusService` (WS1), `ScheduleIndex` (WS1), `LegendIndex` (WS1), `MeasureService` (WS3: `measure_polyline`, `synthesize_routes`), `PriceService` (WS6: `lookup`), `ScopePolicyService` (WS5: `rules(topic, item_description, run_id)`), `RowStore` (WS4), `DocumentAccess` and `heartbeat` (WS2). WS2 ships fakes and `Unavailable` defaults that return a typed error result, so nothing blocks on a sibling.

Word APIs (WS1 is the single implementation; WS3, WS4 and WS8 delete their fallback extractors once WS1 step 2 lands):
- `pagefacts.textlayer.positioned_words(pdf_bytes, page_index0) -> PageWords(page_w_in, page_h_in, rotation, words: list[Word], lines: list[Line], text_class)` (pure; for derive, offline eval and scripts).
- `pagefacts.api.page_words(sb, document_id, page) -> tuple[list[Word], list[Line]]` (cached blob, in-process LRU of 64 pages).
- `Word = {t, b: [x0,y0,x1,y1], h (pt), r (0, 90, 180, 270, -1), f (1 shx, 2 bold, 4 hyphen-joined, 8 ocr)}`; `Line = {t, b, w: [i0, i1], h}`.

Other cross-workstream Python APIs (signatures are normative; implementers may add keyword args):
- WS1 `pagefacts.api`: `doc_facts`, `page_facts`, `sheet_index`, `resolve_sheet`, `legend_lookup`, `schedule_lookup`, `keynote_lookup`, `tag_occurrences`, `tag_regions(... pad_in=1.0, max_regions=4)`, `panel_locations`, `views`, `spec_lookup`, `page_text_block(facts, doc, max_tokens=2500)`, `ledger_facts(sb, documents)`, `note_blocks(sb, document_id, page)`; all raise `FactsUnavailable` instead of fabricating.
- WS4 `plans_reader_rows.iter_rows`, `get_row`; `plans_reader_merge.consolidate_category(category, groups, *, words_by_page, ledger, prior=None)`; `plans_reader_regions.regions_for_flags(...)`, `for_recount(sheet_key, family, floors, words)`; `plans_reader_floors.compute_floors(...)`, `accounted(...)`; `plans_reader_lines.compute(line, rows)`, `parse_conductors(text)`; `plans_reader_gates.pre_consolidation(...)`, `run_final(...)`, `pricing_allowed(run) -> (bool, reasons)`.
- WS3 `plans_reader_measure.build_measure(inputs, conv) -> MeasureOutput`, `measure_summary_text`, `apply_measure_to_boq(boq_spec, measure)` (legacy mode only), `branch_coverage_findings`; `plans_reader_geometry.parse_scale_text`, `detect_viewports`, `register_sheets`; `plans_reader_routes.synthesize_routes`.
- WS5 `scope_engine.prepare(sb, run, outputs, *, allow_llm)`, `render_digest(ctx)`, `enforce(main_output, ctx, policy, overrides)`, `enforce_for_run`, `facts_for_run(sb, run_id) -> list[Fact]`, `classify_obligation(row_or_line) -> {party, carry, reason}`, `rules_text(ctx) -> str`, `evaluate(rows, ctx) -> list[PolicyHit]`, `standards_for(sb, run_id)`, `proposal_lines(report)`; `bid_conditions.classify`, `recheck_threshold(bid, priced_total_usd)`, `for_run`.
- WS6 `pricing_types` (PriceRow with `line_id`, PricingContext, ValueStat, PricedLine, VerifyDraft, PricedEstimate), `cost_library.CostLibrary.load(sb, *, exclude_projects=frozenset())`, `pricing_engine.compute_estimate(rows, library, policy, quotes, ctx, *, exclude_project=None)`, `verify_totals(...)`, `price_estimate(rows, ctx, *, library=None, policy=None, quotes=())` (pure wrapper, all I/O injected), `price_run(sb, run_id, *, boq_version=None, user=None)`, `store_version(sb, run_id, priced, instruction)`, `to_priced_estimate_v1(priced) -> dict`, `pricing_context_from_scope(sb, run) -> PricingContext`.
- WS7 `plans_reader_recount.request_recounts(sb, run, targets) -> list[group_row]`, `select_targets(...)`; `plans_reader_review.begin(sb, run, *, pricing_version)`, `advance(sb, run)`, `result_for(sb, run_id)`, `review_brief_block(result, include_money)`; `plans_reader_ops.apply_ops(boq_spec, ops) -> (spec, applied, rejected)`; `plans_reader_bands.check_bands(...)`.
- WS8 `plans_reader_evidence.build(sb, run, *, main_version, boq_version) -> EvidenceOutput`, `evidence_to_marked_spec(evidence)`; `plans_reader_autonomy.status(sb, run, *, include_pricing)`, `recompute(sb, run_id) -> str`.
- WS0 `eval_manifest.excluded_projects(purpose, case_id=None)`, `eval_ledger.set_call_estimate(usd)`, `register_stage(...)`, `register_check(...)`.

### 5.4 Schemas

Category output v2 (WS4; code adds, never models): every element of counts, runs, schedules, specs, scope, details, questions gains `row_id`, `fingerprint`, `origin {group_id, group_kind, pass, local_id}`, `coverage` (`sheet_total | region_subtotal | tile_subtotal | partial | schedule | stated | derived | unknown`), `status` (`active | superseded | alternate | unverifiable`), `superseded[]`, `alternates[]`, `item_key`, `sheet {document_id, page, sheet_number, sheet_key "D1P8"}`, `positions [[x,y]]`, `family_tags`, `flags`. Top level: `rows_version: 2`, `merge_log`, `floors`, `row_seq`.

Model-facing schema changes (strict-compatible, all nullable):
- `COUNT_ITEM` gains `points: [[x, y]] | null` (4 decimals, page fractions, owner WS4).
- `ESCALATION_COUNT_ITEM = COUNT_ITEM + {supersedes_row_id: str|null, coverage: "whole_sheet"|"inside_tiles_only"}`; `ESCALATION_SCHEMA` uses it (WS4).
- `RUN_ITEM` gains `route: [[x,y]] | null` (WS3).
- `SUB_PAGE` gains `viewports: [{title, scale_text|null, region}] | null` (WS3).
- `SCHEDULE_ITEM` gains `schedule_id: str|null`, `source: "text_layer"|"vision"` (WS1).
- `RECOUNT_SCHEMA`, `REVIEW_SCHEMA`, `REVISE_SCHEMA` and the typed op union (WS7, `plans_reader_review_schemas.py`): section 5.8.
- `MAIN_AGENT_SCHEMA` and `BOQ_ITEM` stay UNCHANGED for the legacy model call. The BOQ line v2 fields (`line_id`, `source_refs [{ref, multiplier}]`, `combine sum|max|single|schedule_value|conductor_footage|floor|manual`, `basis`, `needs_recount`) are validated only by a code-side assembly schema (`BOQ_ITEM_V2_ASSEMBLY`); the renderer ignores unknown keys. `combine: manual` is allowed only for human modify or override with a user id, and manual lines make `pricing_allowed` false until approved.

Outputs (`plans_reader_outputs.scope`): `category`, `main`, `policy` (WS5 ScopeContext), `measure` (WS3 MeasureOutput, versioned writer for overrides), `evidence` (WS8, upserted by (run, scope, version) with `inputs_hash`).

`MeasureOutput v1` (WS3): `{version, conventions, wiring_method, scales[view-level], registrations[], panels[], circuits[], routes[{circuit_id, total_ft, basis, caveat, segments[{kind collector|homerun|tie_in|drop|allowance|switch_leg, length_ft, conductors, raceway, document_id, page, polyline, ft_per_in, drawn bool}], flags}], linear_items[], measured_runs[], quantities[{row_id "M<n>", group, subgroup, description, quantity, unit "FT", wastage_applies, basis, caveat, sheet_refs, derivation{terms}, line_hint}], bands, findings[], judgments[], overrides[], stats}`. Positions as page fractions.

`ScopeContext` / `ScopeReport` (WS5): context = {policy_id, policy_sha, bid_conditions, documents roles, obligations, facts[F*], standards, breaker_plan, inclusions, device_facts, digest}; report (in main output key `scope_policy`) = {decisions[{key, rule_id, action, subject, before, after, evidence, clarification}], clarifications, exclusions, proposal_lines, warnings, needs_review, overrides_applied, counts}.

`BidConditions` (WS5): `{wage_type, wage_basis project_record|document_cue|owner_type|owner_lexicon|default, wage_confidence, federal_wage, pw_score, cues[{cue, weight, document_id, page, quote}], labor_time day|night|mixed (from `projects.labor_time`: `day_work` -> day, `night_work` -> night, null -> from documents, else day with needs_review; phasing or after-hours language -> mixed), union union|non_union|unknown, bond_required bool|null, owner_type, rate_table_key pw|open_shop, needs_review[]}`.

`PricedEstimate` (WS6, `pricing_types.py`, imported by WS7 and WS8): `status priced|provisional|blocked`, `lines[PricedLine{line_id, row (PriceRow), item_key, family, uom, qty_eff, labor ValueStat, material ValueStat, modifiers, material_source library|seed|quote:<id>|fallback|none, labor_hours, material_cost, pricing_category, pricing_section, flags, candidates}]`, `categories`, `verify VerifyDraft|None`, `workbook_basis`, `range_low`, `range_high`, `quality_summary`, `unpriced {lines, est_hours, est_material}`, `blockers`, `warnings`, `policy_version`, `library_fingerprint`, `excluded_project_ids_sha256`. `ValueStat {value, p25, p75, n_obs, n_projects, latest_seen "YYYY-MM", quality, method exact|compatible|context|seed|quote|fallback|none}`. `VerifyDraft` = the ten `VERIFY_NUMBERS` keys plus `bid_total, labor_hours_basis, labor_hours_reviewer, labor_rate, labor_rate_source`.

WS0 artifact `bdr.priced_estimate/1` (from `to_priced_estimate_v1`): top `refused, refusal_reasons, wage_type, labor_rate_usd, labor_rate_basis`; `lines[{line_id, section, group, subgroup, description, kind, unit, qty_base, wastage, qty, quantity_basis, unit_hours, unit_material_usd, hours_source library|policy|manual, material_source library|policy|vendor_quote|allowance|manual|none, n, p25, p75, quote_ref, priced, excluded, exclusion_reason}]`, `allowances`, `quotes[{category, vendor, amount_usd, tax_included, replaces_sections, file_sha256}]`, `sections`, `totals {hours_raw, hours, hour_roundup, labor_usd, material_pretax_usd, tax_rate_pct, material_usd, verify{ten keys}, bid_total_usd}`, `exclusions`, `clarifications`, `metadata {excluded_project_ids_sha256, library_fingerprint}`. Invariant: `bid_total_usd == sum(verify)` with None as 0. `refused` is true when status is `blocked`. Source mapping (`to_priced_estimate_v1`): `hours_source` from labor `ValueStat.method`: exact, compatible, context, fallback -> `library`; seed -> `policy`; a human or override value -> `manual`; none -> `priced: false`. `material_source` from `PricedLine.material_source`: library, fallback -> `library`; seed -> `policy`; `quote:<id>` -> `vendor_quote` with `quote_ref = <id>`; allowance rows -> `allowance`; human value -> `manual`; none -> `none`.

WS0 artifact `bdr.gate_report/1` (from WS4 GateReport): `{blocking, checks[{code, severity block|warn, detail, rows[line_id]}]}`. When `blocking` is true, the priced artifact has `refused: true`.

Gate ids (WS4, the only names): `new_devices_no_branch`, `linear_in_ea`, `census_floor`, `partial_basis`, `unverifiable_citation`, `unsourced_line`, `orphan_rows`, `arithmetic`, `unit_class`, `excluded_system` (`fact_conflict`), `brief_numbers`, `capability_denial`, `lost_sheets`, `double_consumption`, `uom_conflict_quote` (raised by WS6, recorded by WS4), `branch_ratio` (warn). WS3 findings map onto these (`branch_ft_empty` -> `new_devices_no_branch`).

`ReviewResult` (WS7): `{review_id, run_id, base_pricing_version, revised_pricing_version, verdict clear|advisory|needs_human|incomplete|skipped|stale, reasons, findings[ReviewFinding], bands[BandCheck], cost_usd, models, stages}`.

`AutonomyStatus` (WS8): `overall ready_for_verification|needs_review|blocked|in_progress|not_evaluated`, checks `reading_complete, integrity_gates, lengths_produced, scope_conditions, pricing_coverage, independent_review, history_bands, evidence`, sign-off. Persisted as `plans_reader_runs.autonomy_state` by `autonomy.recompute` after every post-run step and every modify.

### 5.5 Roles and visibility (WS8 day-1 deliverable; everyone imports)

- `app/core/roles.py`: `AI_PRICING_VIEW_ROLES = {EXECUTIVE, IT_ADMIN, ESTIMATING_ADMIN} | ESTIMATING_ENGINEER_ROLES` (Accountant excluded, Q7; estimator never); `AI_PRICING_WRITE_ROLES = {EXECUTIVE, IT_ADMIN, ESTIMATING_ADMIN}`; `AI_PRICING_POLICY_ROLES = VERIFY_ROLES`.
- `app/core/features.py`: `ai_pricing_allowed(role)` = `ai_estimating_allowed(role)` and `ai_pricing_enabled` and role in `AI_PRICING_STATE_ROLES[ai_pricing_state]` (`test`: Executive and IT Admin; `live`: AI_PRICING_VIEW_ROLES) and role is not ESTIMATOR. `/features` adds `ai_pricing` per caller.
- `app/core/deps.py`: `require_pricing_view` (404 when `ai_pricing_enabled` is false, 403 when the role is not allowed), `require_pricing_write`.
- Chat: the pricing clause in `_SYSTEM_RULES` (`plans_reader_chat.py:1202`) becomes conditional on the SENDER's role, resolved per message from `created_by` and persisted as `sender_role` on the message row. Assistant messages and tool-call records produced with an `internal_pricing` tool are tagged `pricing_restricted`; `list_threads`, `get_thread` and the message routes redact their content for callers who fail `ai_pricing_allowed` (test with the Accountant and with an engineer under `ai_pricing_state=test`).
- Tool results from `price_lookup` and `get_price_summary` never contain project ids, project names, GC names or addresses.
- Dev testing of role matrices patches settings (`AI_ESTIMATING_STATE=live`, `AI_PRICING_STATE=live`); the dev `.env` stays `test` for the human UI.

### 5.6 Tables and migrations (placeholders, numbered at build time; dev only)

Order of application = letter order. Every new table: `enable row level security` and `force row level security`, no policies, revoke from anon and authenticated; `notify pgrst, 'reload schema'` after each. Read live constraint definitions (`pg_get_constraintdef`) before any drop and re-add.

- MA `ai_eval_ledger` (WS0): `ai_eval_budget` (single row, ceiling_usd <= 400, epoch_at, enforce), `ai_eval_allocations` (workstream pk, allocated_usd, note), `ai_eval_sweeps` (label, workstream, purpose check dev_probe|smoke|core|holdout|noise|ab, budget_usd, status open|closed|aborted, opened_at, closed_at, spent_usd, code_version, prompt_hash, config, cases, result_path; unique partial index: one open sweep). MA holds only the `ai_eval_*` tables, which only eval code reads (guarded by `ai_eval_ledger_enforce`), so MA never ships. Applied in wave 1 (section 7).
- MB `plans_reader_page_facts` (WS1): `plans_reader_documents` add `sha256`, `facts_status` (pending|running|done|failed|skipped), `facts_error`, `category_label`, `facts_doc` jsonb (per-document results: category_check action, relabel, spec sent flags, splitter-title fallbacks); tables `plans_reader_doc_facts` (pk sha256, kind drawing|text, extractor_version; facts jsonb, blob_prefix, stats) and `plans_reader_page_facts` (pk sha256, kind, extractor_version, page; sheet_number, sheet_title, discipline, page_role, roles text[], text_class, primary_ft_per_in, facts jsonb; fk cascade; index on sheet_number).
- MC `plans_reader_autonomy_core` (integrator; the ONE migration that rewrites existing CHECKs, including the two that ME and MG would otherwise touch): columns that shipped code reads or writes and that therefore must ship with MB to MG: `llm_call_log.eval_sweep_id uuid` plus partial indexes on `(eval_sweep_id)` and on `(created_at) where cost_usd is not null`, `plans_reader_runs` add `eval_case_id text`, `eval_sweep_id uuid` (all nullable; code also omits these keys from inserts and selects when the value is null, tested with the columns absent); `plans_reader_groups.kind` CHECK = sub, rollup, escalation, main, modify, chat, derive, recount, review, revise; groups add `mode` (read|review|write), `route jsonb`, `loop_state jsonb`; `plans_reader_outputs.scope` CHECK = category, main, policy, measure, evidence; `llm_jobs.job_type` CHECK = current live list plus `plans_reader_facts`, `ai_pricing`; `plans_reader_deliverables.kind` CHECK adds `annotated_plans` (for WS8); `file_access_events.source` CHECK (0161:28) adds `ai_pricing_estimate`, `ai_pricing_quote` (for WS6); `plans_reader_calls` add `provider`, `round_no`, `cache_write_tokens`, `conservative_cost bool default false`, `message_id uuid`, index on `(group_id)`; `plans_reader_runs` add `estimated_cost_usd_max numeric`, `autonomy_state text not null default 'not_evaluated'` (CHECK not_evaluated|in_progress|blocked|needs_review|ready_for_verification), `gate_summary jsonb`; tables `plans_reader_loop_rounds` and `plans_reader_tool_calls` (WS2 shapes: group_id or message_id, round_no, status sent|done|failed|abandoned, request_sha256, est_usd, response_id, assistant_raw, tool_calls, usage, cost_usd, call_id; partial unique index on (coalesce(group_id, message_id), round_no) where status <> 'abandoned'; tool calls unique (coalesce(group_id, message_id), call_id), result_text, result_data, citations, views); table `plans_reader_gate_findings` (WS4 shape: run_id, output_version, stage pre|final|pricing, gate_id, severity, route recount|measure|model_fix|human|none, status open|routed|resolved|overridden|superseded, target, message, evidence, fingerprint, routed_group_id, resolved_by, resolved_at, note; unique (run_id, output_version, stage, fingerprint)); `plans_reader_chat_messages` add `sender_role text`, `pricing_restricted bool default false` (verify the real table name).
- MD `ai_scope_policy` (WS5): `ai_scope_policies` (version unique, status draft|active|archived, partial unique index on active, doc, sha256, based_on, notes, created_by, activated_by/at); `plans_reader_runs` add `scope_policy_id`, `scope_policy_sha`, `bid_conditions jsonb`; `plans_reader_scope_overrides` (run_id, decision_key, action include|exclude|accept, note, created_by; unique (run_id, decision_key)).
- ME `ai_pricing` (WS6): `cost_sources`, `cost_items` (pg_trgm GIN on search_text), `cost_observations`, `ai_pricing_settings` (single row, policy jsonb, version), `vendor_quote_extractions`, `plans_reader_pricing` (run_id + version unique, boq_version, status, policy snapshot, library fingerprint, context, categories, verify_draft, workbook_basis, range, quality_summary, unpriced, blockers, warnings, storage_path, stale bool), `plans_reader_priced_lines` (pricing_id + line_id pk, line jsonb). (The `file_access_events.source` values are added by MC.)
- MF `plans_reader_review` (WS7): `plans_reader_reviews` (run_id, base_pricing_version, revised_pricing_version, mode, stage, verdict, reasons, result, models, budget_usd, cost_usd, band_version, error; unique (run_id, base_pricing_version)), `plans_reader_review_findings` (with human_label, note, labeled_by, labeled_at), `plans_reader_recounts` (target_id, row_id, document_id, page, priority, builder_qty, recount_qty, census_floor, tiebreak_qty, mean_conf, outcome, marks, model, group_id), `ai_history_bands` (band_version, metric, stratum, n, p10 to p90, values jsonb per project, manifest_sha256, active; pk (band_version, metric, stratum)).
- MG `plans_reader_evidence_signoff` (WS8): table `plans_reader_price_signoffs` (run_id, pricing_version, decision verified|rejected, note, autonomy_snapshot, totals_snapshot, created_by, created_at). The `annotated_plans` deliverable kind is added by MC.

The priced workbook is NOT a deliverable kind: it lives at `plans_reader_pricing.storage_path` and is served only by the pricing router.

### 5.7 Flags and settings (code default -> dev `.env`)

All in `app/core/config.py` with validators, in `.env.example` with a one-line comment, and snapshotted per run by `_settings_snapshot` (which also gains `prompt_hash`, `code_version`, `routes`, `conventions`). One row per setting; "same" means the dev `.env` does not set it.

Rule for defaults: every PARENT flag (one that turns a behavior on) is off in code. A few CHILD settings default true in code; they are inert unless their parent is on, and each names its parent below. `anthropic_plans_refusal_fallback` defaults true for the same reason: it only acts on a call made on an Anthropic route, and no Anthropic route runs unless a parent flag (`plans_reader_recount_enabled`, `plans_reader_review_mode`) is on and the route names Anthropic. No setting below changes prod behavior on deploy while the parents stay off.

| Setting | Code | Dev | Owner | Parent or note |
|---|---|---|---|---|
| `plans_reader_page_facts_enabled` | false | true | WS1 | parent |
| `plans_reader_page_facts_in_subs` | true | same | WS1 | child of page_facts_enabled |
| `plans_reader_spec_select` | true | same | WS1 | child of page_facts_enabled |
| `plans_reader_facts_relabel` | false | true | WS1 | child of page_facts_enabled |
| `plans_reader_facts_title_vision` | false | false | WS1 | vision title fallback, deferred |
| `plans_reader_facts_page_budget_s` | 4 | same | WS1 | per page time cap |
| `plans_reader_facts_doc_budget_s` | 240 | same | WS1 | per document time cap |
| `plans_reader_facts_block_max_tokens` | 2500 | same | WS1 | per-page facts block cap |
| `plans_reader_ledger_legend_max_tokens` | 6000 | same | WS1 | must be < `plans_reader_ledger_max_tokens` |
| `plans_reader_ledger_schedule_max_tokens` | 1500 | same | WS1 | panels and luminaires, each |
| `plans_reader_ledger_scales_max_tokens` | 300 | same | WS1 | scales block |
| `plans_reader_routes` (JSON: per step provider, model, effort, budget, tools) | code defaults of section 8.3 | same | WS2 | read only by kinds whose parent is on |
| `plans_reader_run_ceiling_factor` | 1.25 | same | WS2 | |
| `anthropic_plans_refusal_fallback` | true | true | WS2 | inert unless an Anthropic route runs (see rule above) |
| `plans_reader_derive_enabled` | false | true | WS3 | parent of the derive group (WS5 `prepare` runs inside it) |
| `plans_reader_branch_conduit` | `3/4"C EMT` | same | WS3 | convention |
| `plans_reader_min_branch_awg` | 12 | same | WS3 | convention |
| `plans_reader_vd_max_pct` | 3.0 | same | WS3 | convention |
| `plans_reader_route_factor` | 1.00 | same | WS3 | calibrated in B9, owner signs off |
| `plans_reader_drop_ft_per_device` | 0 | same | WS3 | Q17 |
| `plans_reader_panel_rise_ft` | 0 | same | WS3 | Q17 |
| `plans_reader_existing_tie_in_ft` | 25 | same | WS3 | convention |
| `plans_reader_homerun_default_ft` | 150 | same | WS3 | replaced by the calibrated golden median |
| `plans_reader_floor_to_floor_ft` | 15 | same | WS3 | convention |
| `plans_reader_receptacles_per_circuit` | 6 | same | WS3 | convention |
| `plans_reader_control_cable_ft_per_device` | 25 | same | WS3 | convention |
| `plans_reader_measure_conventions_version` | `g3-branch-2026-10-a` | same | WS3 | bumped to `-b` after B9 sign-off |
| `plans_reader_measure_assist_enabled` | false | true | WS3 | parent |
| `plans_reader_measure_assist_max_items` | 8 | same | WS3 | child |
| `plans_reader_measure_assist_max_usd` | 0.75 | same | WS3 | child, per run |
| `plans_reader_consolidator_mode` (`tools` or `legacy`) | legacy | tools | WS4 | parent |
| `plans_reader_consolidator_max_rounds` | 20 | same | WS4 | |
| `plans_reader_consolidator_max_tool_calls` | 400 | same | WS4 | |
| `plans_reader_consolidator_max_usd` | 6.00 | same | WS4 | |
| `plans_reader_llm_rollup` | false | false | WS4 | |
| `plans_reader_code_regions` | false | true | WS4 | parent (code-computed escalation regions) |
| `plans_reader_region_pad` | 0.03 | same | WS4 | child, page fraction |
| `plans_reader_cluster_gap` | 0.06 | same | WS4 | child, page fraction |
| `plans_reader_census_tol_strong`, `plans_reader_census_tol_weak` | 0.05, 0.10 (minimum 1) | same | WS4 | |
| `plans_reader_gates_blocking` | false | true | WS4 | parent |
| `plans_reader_demo_two_line_style` | false | set by rule | WS4 | dev value: true when the WS4 offline style check finds the two-line demolition style (one lighting removal row, one power device removal row) in >= 60% of golden estimator BOQs that have demolition, else false; record the share |
| `plans_reader_recount_enabled` | false | true | WS7 | parent |
| `plans_reader_recount_max_targets`, `plans_reader_recount_max_sheets`, `plans_reader_recount_budget_usd` | 12, 6, 2.00 | same | WS7 | children |
| `plans_reader_review_mode` (`off`, `shadow`, `apply`) | off | apply | WS7 | parent |
| `plans_reader_review_budget_usd` | 4.00 (max 6.00) | same | WS7 | |
| `plans_reader_review_local_rules_fallback` | false | false | WS7 | when true, WS7 uses its own copies of WS4 gate and WS5 decision rules (only while those modules are absent) |
| `ai_scope_policy_enforce` | false | true | WS5 | parent |
| `ai_scope_attrib_llm` | false | true | WS5 | parent of the attribution fallback call |
| `ai_scope_attrib_max_statements` | 40 | same | WS5 | |
| `ai_scope_digest_max_tokens` | 3500 | same | WS5 | |
| `ai_pricing_enabled` | false | true | WS6 | parent |
| `ai_pricing_state` (`test` or `live`) | test | test | WS6 | WS8 reads it for roles |
| `ai_pricing_autorun` | false | true | WS6 | |
| `ai_pricing_chat_tools_enabled` | false | true | WS6 | |
| `ai_pricing_agent_tools_enabled` | false | true | WS6 | |
| `quote_lines_llm_fallback_enabled` | false | true | WS6 | |
| `plans_reader_marked_plans_mode` (`code` or `model`) | model | code | WS8 | |
| `plans_reader_annotated_plans_enabled` | false | true | WS8 | |
| `eval_root` | `/Users/thomasmoore/Desktop/BDR/backups/eval` | same | WS0 | |
| `ai_eval_ledger_enforce` | false | sweep backend and live scripts only | WS0 | |
| `llm_cassette_mode` (`off`, `record`, `replay`, `record_missing`, section 0.4) | off | sweep backend and scripts only | WS0 | |
| `llm_cassette_dir` | empty | `$EVAL_ROOT/cassettes` | WS0 | |
| `eval_sweep_id` | empty | set per process | WS0 | stamped into `llm_call_log` by `llm_gate.log_call` |
| `llm_queue_claim_types` (comma list; empty = all types) | empty | sweep backend only | WS0 | restricts what a worker claims |

Live sweeps are leave-one-project-out: when `plans_reader_runs.eval_case_id` is set, the pricing library, scope policy mining, commonly-missed list and history bands are built with `eval_manifest.excluded_projects(purpose, case_id)`, and the priced artifact records `excluded_project_ids_sha256` (acceptance check GC4 verifies it includes the case's own project).

### 5.8 Model-facing write tools and review schemas (normative)

All args follow the strict rules of 5.2 (every property required, optional ones nullable, `additionalProperties: false`). Every write returns either a result object or a structured refusal `{refused: true, code, message, detail}`; a refusal never changes state.

Consolidator write tools (WS4, kind M only; idempotent by `group_id:call_id`):

| Tool | Args | Result and refusal codes |
|---|---|---|
| `add_boq_line` | `area`, `section`, `group`, `subgroup`, `description`, `unit` (UNITS), `source_refs [{ref, multiplier}]`, `combine` (sum, max, single, schedule_value, conductor_footage, floor), `caveat` or null, `material_note` or null, `material_category` | `{line_id, quantity, wastage_applies, sheet_refs, basis, warnings}` computed by `plans_reader_lines.compute`. Refusals: `unknown_ref`, `unverifiable_ref`, `superseded_ref`, `unit_mismatch`, `ref_consumed` (names the consuming line), `partial_requires_floor` (a ref with coverage tile_subtotal or partial and combine other than floor), `excluded_by_fact` (quotes the verified fact), `vocab` (group or subgroup not in the BOQ vocabulary), `conduit_form` (conduit description not in `_CONDUIT_RE` form) |
| `accept_candidates` | `ids` [K ids], `overrides` [{`id`, `description`, `group`, `subgroup`, `caveat`, `material_note`}, each field nullable] | each candidate goes through `add_boq_line`; per-id results |
| `update_boq_line` | `line_id`, then every `add_boq_line` field as a nullable property (null = unchanged) | recomputed line; same refusals |
| `remove_boq_line` | `line_id`, `reason` | `{line_id, freed_refs}`; consumption released, logged |
| `account_rows` | `row_ids`, `disposition` (existing_to_remain, existing_circuit, by_others, other_trade, duplicate_view, not_electrical, info_only, superseded_by_schedule), `reason`, `fact_id` or null, `duplicate_of` or null, `note_ref` or null | `{accounted}`. Refusals: `duplicate_view` without `duplicate_of` of the same item_key on another sheet; `by_others` without a verified fact (becomes a human finding); `existing_circuit` without a cited note |
| `add_allowance` | `name`, `cite` {`row_id` or null, `fact_id` or null} | refused when the name's system is excluded by a verified fact or the scope policy, or the cite does not resolve |
| `add_exclusion`, `add_assumption`, `add_rfi`, `add_risk` | `text`, `cite` (row id, fact id, ledger sheet number or spec TOC section) | refused when the cite does not resolve |
| `write_brief` | `field` (project_summary, electrical_scope_summary, systems, other_trades_affecting_electrical, sheet_summaries, spec_highlights, missing_documents), `value` | prose checks: numeric claims must equal app totals (`brief_numbers`); no `$`, `hours`, `hrs`, `MH`; capability-denial phrases refused when the measure output has data (`capability_denial`) |
| `set_estimate_notes` | `general_notes`, `area_notes` | same prose checks |
| `set_material_notes` | `notes` [MATERIAL_NOTE] | vocab check |
| `finish` | `confidence_notes` | runs the final gates; returns `{passed, findings (model_fix only), blocking_routed}`; callable at most 3 times, the loop ends on pass or the third call |

Measure write tools (WS3, kinds D and C; chat needs a writer role and change words):

| Tool | Args | Result |
|---|---|---|
| `set_linear_item` | `document_id`, `page`, `tag`, `candidate_ids` [str], `endpoints` [[[x,y],[x,y]]] or null (page fractions), `view_id` or null | new measure output version with an `overrides` entry; `{total_ft, instances [{id, length_ft}]}` |
| `set_panel_location` | `panel`, `document_id`, `page`, `view_id` or null, `point` [x,y] | new measure output version with an `overrides` entry; `{routes_changed, delta_ft}` |

`RECOUNT_SCHEMA` (WS7 recount output): `{targets: [{target_id, marks: [{tile_id, x, y, confidence}], uncertain_regions: [{tile_id, x0, y0, x1, y1, reason}], symbol_seen_in_legend: bool, notes}]}`. Code maps marks to page fractions, keeps a mark only inside its tile's owned core (tile ownership), and resolves outcomes per WS7 (confirmed, raise, lower, unresolved).

`REVIEW_SCHEMA` (WS7 reviewer): `{summary, findings: [{ref, kind, severity, title, detail, line_ids [B ids], evidence: [{type, document_id or null, page or null, quote or null, ref_id or null}], proposed_ops: [OP], qty_delta_hint or null, confidence}], systems_checked: [{system, status, line_ids}]}` with `kind` in missing_scope, excluded_scope_carried, quantity_suspect, unit_wrong, price_outlier, band_outlier, spec_conflict, bid_condition, double_count, false_rfi, other; `severity` in critical, warning, info; evidence `type` in page_quote, band, rule, recount, tool; `status` in covered, missing, excluded, unclear. Code validation: unknown line ids dropped; a `page_quote` must match the cited page text (`difflib` ratio on normalized text, >= 20 chars, contains a key term); `tool` evidence must reference a call in the group's trace; a finding with no verified evidence is capped at info and its ops dropped; severity is recomputed from code-computed impact (the model's severity is input only).

`REVISE_SCHEMA` (WS7 revise): `{dispositions: [{finding_id, decision (apply, modify, decline), reason, evidence: [EVIDENCE], ops: [OP]}]}`. A finding without a disposition becomes `open`.

`OP` (typed op union, discriminated by `op`; `plans_reader_ops.py`; all keyed on BOQ `line_id`):

| op | Fields | Precondition |
|---|---|---|
| `set_quantity` | line_id, from_qty, to_qty, basis, note | line exists; from_qty equals current within 1e-6 |
| `set_unit` | line_id, from_unit, to_unit, to_qty, basis | current unit equals from_unit; to_unit in UNITS; applies only when to_qty comes from a measurement tool result or a verified quote, otherwise the line stays and the finding routes to human |
| `add_row` | area, section, group, subgroup, description, qty, unit, basis, sheet_refs, item_key or null | vocabulary; EA whole numbers |
| `remove_row` | line_id, from_qty, reason_code (by_others, not_used, policy_exclusion, duplicate, not_in_scope) | line exists; qty matches |
| `set_flag` | line_id, flag (excluded, by_others, alternate), policy_rule_id or null | line exists |
| `add_allowance`, `remove_allowance` | name, reason_code | allowance vocabulary or a policy-approved name |
| `set_condition` | key (wage_type), from_value, to_value | current value equals from_value |
| `add_clarification` | text (<= 300 chars) | none |
| `add_rfi` | question, document_id, page | page exists |

Ops of one finding apply all or none. Evidence rules (decreases need two signals or a policy hit; additions need a verified quote, recount raise, census floor or a measurement tool result; wage changes need a verified NRS 338, prevailing wage, Davis-Bacon or public works quote; at most 40 ops per review; a total move over 15% adds `large_revision`) are in WS7.

Scope policy document (WS5, `ai_scope_policies.doc`, seeded by `scope_policy_seed.py`): `{version, bid_conditions: {rate_table, default_wage_type}, standards: {raceway, branch_conduit, demo_disposition, ...}, rules: [{rule_id, topic, kind (hard or soft), decision (include, exclude, allowance, by_others), match {patterns [regex], systems [str], exempt_subgroups [str], exempt_patterns [regex]}, unless [condition] (soft rules only; validation refuses `unless` on hard rules), default_text, proposal_line, evidence}], inclusions: [{trigger_family, pattern, line_template, dedupe_scope}], allowances: {standard [9 names], extra_requires_document_amount: true}}`. Seed content: the 43-of-43 sent-proposal exclusions (temporary power, the template exclusion sentence, "Any other scope not enlisted above to be performed by others.", "Add 3% if Payment and Performance bond is required."), the E5 house-exclusion topics, Q12 cutting and patching, Q14 thermostats and control cable, the raceway and demolition standards of WS5. The validator is `scope_policy.validate(doc)`; a policy that fails it cannot be activated.

---
## 6. Workstreams

Each block: purpose and errors, design (only what is not already in sections 4 and 5), files, build steps, tests, acceptance, live spend, risks. Tests are $0 unless marked live.

### WS0 Eval harness, scorer, ledger, replay (fixes E25; the measuring stick)

Design. `EVAL_ROOT` (outside git) holds manifest, references, adjudications, bundles, cassettes, replays, sweeps, reports. `eval_manifest.py` (tiers section 8.1, `blind_guard`: refuses answer-hash, answer-category, answer-name uploads and quotes in a quantity stage; `excluded_projects`). `eval_reference.py` builds `reference.json` per case from the backup using the truth-source map of section 8.6 (BOQ, estimate lines joined to BOQ, recap, G3 sent, Verify, labor notes "Labor Hours = 625 / Labor Rate = $125", quotes, adjudications applied; data-quality checks: Verify sum equals sent on 37 of 39, materials equal taxed quotes 39 of 39). Scorer v2 `plans_reader_score.py` (MATCHER_VERSION 2): `classify_line` kinds with aliases, many-to-one `assign` (AI more specific is a child; never crosses kind or identity; unit mismatches counted, not summed), metrics reported as count lines exact / count lines total, length bands (good <= 10%, acceptable <= 25%), dollar coverage and weighted error, bid totals vs G3 sent, corrected and estimator. Refused estimates count as misses. Replay `plans_reader_replay.py` with pure stages that mirror the spine of section 4.1 (`STAGE_ORDER = plan, merge, recount, derive, consolidate, enforce, gate, evidence, render, price, review, score`; owners: WS1 plan, WS4 merge, WS7 recount (stored or cassette only), WS3 and WS5 derive, WS4 consolidate (stored, cassette, or legacy code assembly), WS5 enforce, WS4 gate, WS8 evidence, WS0 render (calls `render_boq_xlsx` and `render_estimate_xlsx` directly with `office_pdf_bytes` patched out), WS6 price, WS7 review (rules lane rerun, model stages stored), WS0 score; stage functions take `ctx.sb is None` and read documents through `ctx.docs`). No hybrid live mode: a live probe is a real dev run on a page-subset PDF whose bundle is exported and replayed. Cassette at the transport boundary: content-addressed store `cassettes/<sha256>.json` plus a sweep index; a hit sets `_last_usage` (tagged cassette) and `_last_cost = 0`, writes no `llm_call_log` and no cost into `plans_reader_calls`; stable run names `EVAL <case>` (sweep id only in `eval_sweep_id`). Ledger `eval_ledger.py`: guard in `llm._guarded` refuses `no_open_sweep`, `sweep_budget`, `global_ceiling`, `unpriced_model`, `allocation`; in-process in-flight reservation (admit when spent + inflight + reserve <= limit; release in finally); reserve = request-derived max (input estimate x price with the over-272k multiplier + max_tokens x output price; callers set it with `set_call_estimate`, and `_call_model` sets it from est_usd); attribution by `llm_call_log.eval_sweep_id` stamped at log time; the sweep backend runs ONE uvicorn process; cases whose run ends with a budget refusal are `budget_aborted` and never scored. Acceptance suite `plans_reader_acceptance.py` implements exactly the rows of section 3.5 (E1 to E25 and GC1 to GC4) with the metrics of section 3.6; `register_check` lets workstreams add checks but never change a 3.5 threshold. Checks that need a text layer skip pages with no text and tagless devices (use estimator marked-plan positions as anchors where they exist). Static test: no provider client construction (`openai.OpenAI(`, `OpenAI(`, `anthropic.Anthropic(`, `Anthropic(`, `AsyncOpenAI(`, `AsyncAnthropic(`) and no provider SDK import under `app/` or `scripts/` outside `app/services/llm.py`, `llm_anthropic.py`, `llm_errors.py` (imports `openai` and `anthropic` for error classification today), `llm_health.py` and `app/services/browser_agents/`. Verified 2026-10-21: those are the only files importing either SDK, so the test passes the day it is written. Queue isolation: `llm_queue._claim_tick` honors `llm_queue_claim_types` (empty = every type); the sweep backend sets it to the plans reader types, `bid_split` (the splitter reads unlabeled drawings) and `ai_pricing`, so it never claims bidding jobs. The ledger guard applies to every call made in a process with `ai_eval_ledger_enforce` on, which with that restriction means plans, splitter, pricing and eval-script features only.

Files: new `app/services/{plans_reader_score,eval_manifest,eval_reference,eval_bundle,plans_reader_replay,llm_cassette,eval_ledger,plans_reader_acceptance,estimating_contracts,plans_reader_prompt_hash,eval_text_anchors}.py`; scripts `eval_{manifest_build,reference_build,bundle,replay,ledger,run,acceptance,report}.py`; `tests/test_no_dashes_ai_estimating.py` and the manifest `tests/ai_estimating_new_files.txt`; `docs/AI_ESTIMATING_EVAL.md`. `scripts/eval_run.py` EXTENDS the existing `scripts/plans_reader_live_run.py` (imports its `_Auth`, `_project`, label mapping, upload and poll code; the old script keeps working unchanged) and adds `--tier`, `--case`/`--cases`, `--sweep`, `--workstream`, `--purpose`, `--budget`, `--dry-run` (stop after estimates, $0), `--no-start` (create and upload only), `--repeat`, `--no-cassette`, `--holdout-final` (refuses on a dirty tree), and sets `eval_case_id`. Changed: `scripts/plans_reader_golden.py` (thin shim re-exporting `main`, `report_markdown` and the moved names), `llm.py` (guard, cassette, fingerprint kwarg), `llm_gate.py` (`log_call` row builder stamps `eval_sweep_id` when set), `llm_errors.py` (`EvalBudgetRefused`, `CassetteMiss` permanent), `llm_queue.py` (`llm_queue_claim_types` in `_claim_tick`), `config.py`, `plans_reader_runs.py` (snapshot fields, eval ids), status route `eval_ledger` block. Migration MA (tables) and the eval columns in MC.

Steps: 0 verify the S0 copy (section 7) and create the S1 layout. 1 move v1 scorer functions (S3 already created `electrical_items.py`); 25 golden tests unchanged. 2 classifier and assignment; then the CLASSIFIER GATE: run `classify_line` over all 66 golden estimator BOQs (pool and core only, never holdout) and require 0 unresolved within-case truth-key collisions (extend attrs with conductor role phase or ground or lug, material, insulation, section_class for conduit and wire, SWITCHED and HALF_SWITCHED, TRIPLEX, SINGLE, SPECIAL amps; or merge colliding truth lines deterministically with a report) and at most 15% of non-accessory lines (and 10% of truth dollars) in `other`; commit the per-case report as a golden test. 3 references and data-quality checks. 4 manifest. 5 adjudications (unreviewed). 6 contracts module. 7 bundles (weekend import, Five Star and YMCA dev exports). 8 replay. 9 acceptance with detector self-tests on the weekend bundle (good-bundle tests added as each workstream lands its artifact). 10 MA, ledger, allocations. 11 cassette; full suite. 12 prompt hash. 13 `eval_run.py` (dry run first). 14 publish the $0 baseline table (every 3.5 row's baseline value measured on the weekend bundle). Step 7 dev bundles: find the Five Star (7172) and YMCA (7177) dev runs with `select id, name, status, created_at from plans_reader_runs where name ilike '%7172%' or name ilike '%Five Star%' or name ilike '%7177%' or name ilike '%YMCA%' order by created_at desc;` on dev (PLANS_READER.md 8.4 describes them; prior costs $25.99 and $6.65), take the latest `done` run of each, record the ids in the build log.

Tests: scorer unit tests (EM aliasing, style words dropped, many-to-one sums 26 + 28 + 3 = 57, never crosses kind, unit mismatch counted, wastage once, dollar formulas, refusal counting), manifest and blind guard, references, ledger (refusals, one open sweep, in-flight reservation under 3 concurrent threads, production refusal), cassette (hit costs 0 in both logs), replay never calls a provider, acceptance pass/fail, `test_miners_call_exclusions`, static SDK test; golden (marked, skipped without EVAL_ROOT): 7154 reference numbers, scorer v2 on the weekend bundle, every E check fails on baseline.

Acceptance: section 3.4 scorer numbers reproduce; classifier gate green; ledger enforced; baseline published. Live: $0 (the shared sweeps are in section 8). Risks: noisy truth (adjudicate), small n (LOPO backtest carries pricing signal), contamination (static test and metadata hash).

### WS1 Page facts (fixes E16, E18, E19, E24; enables everything)

Design. Package `app/services/pagefacts/` (v1 cut): `textlayer` (frame, census, words, lines, stream, SHX annotation harvest, sanitize via `app.sandbox.textclean`, text_class), `regions`, `titleblock` (document-level slot learning plus label, SHX and splitter fallbacks; never invent), `scales` (stream regex; view-bubble and legend-example rules; per-view scale; `views[].region` filled from frame rectangles when detectable, else `scale_conflict` reported and consumers must not measure across views), `roles` (multi-label), `legend` (table and key-value legends, modifiers; ranking by modifiers and device-type relevance first, then `used_on_pages`, so REC-DUP-GFI, REC-TAMP, JB and ECONN survive a 1,000-token cap), `schedules` (finder, panel and luminaire parsers; equipment and feeder parsers deferred to v1.1), `notes` (blocks joined by column: x-overlap plus consistent leading; exact strings tested), `tags` (circuit grammar with optional circuit number, 1-letter panel names only when from a parsed schedule, default-panel notes such as "ALL CIRCUITS TO PANEL LA UON"; status tags; type marks; panel labels; clusters), `specs` (section map, TOC, tier selection, "DIVISION nn NOT USED" captured as facts), `extract`, `store`, `api`, `services` (adapters implementing the WS2 Protocols with refs), `gate`. Panel circuit status uses POSITIVE evidence: `existing_reused` only with an (E) or EXISTING marker on the row or a description matching the demolition-plan load; otherwise `unknown` with signals (bold flag, revision delta near the row, (N) tag, filled wire and VD columns); keyed rows matching NEW/FURNISH AND INSTALL ... BREAKER are `new`. Pages with `vector_text`, `scanned` or `sparse` report `text_layer_quality` and census `unavailable`; optional local OCR is a named follow-up (not in this build). Cache key (sha256, kind, extractor_version) with per-document results on `plans_reader_documents.facts_doc`; relabel applies on hit and miss. Job `plans_reader_facts` is claimed in the interactive tier of `_claim_plans`, renews the lease, takes `_PDFIUM_LOCK` per page, relies on the queue lease for stale jobs, and ENDS by calling `plans_reader_runs.advance(sb, run_id)` for every run in `splitting` with that document (try/except, logged). Integration: `page_meta_from_facts` (persist the merged page_meta, including for labeled sets), `build_ledger` roles plus `ledger_facts` (legend 6,000 tokens, panels and luminaires 1,500 each sent only to drawing-category subs and main, scales 300; legend image becomes a legend-region crop; no general-notes images when notes are present as text), facts block after each page in `_add_document_pages` (a facts provider dict loaded once in `_build_sub`), spec chunks for selected spans plus tier B snippets as a `PackDoc` field with explicit tokens placed once with the document's first group (or in the ledger when a document has no tier A pages), chunks cut at text_class boundaries, relabeled non-electrical drawing documents read only on pages whose roles include schedule or legend or that carry cross-trade electrical mentions. The pre-run cost preview stays pre-facts (documented). FE DocumentTree and the facts route are deferred to v1.1.

Files: the package; hooks in `plans_reader_agents.py` (`build_ledger`, `ledger_text`, `plan_ledger`, `_add_document_pages`, `SUB_SYSTEM` one paragraph), `plans_reader_runs.py` (`_advance_splitting` gate, `_plan_reading`), `plans_reader_pack.py`, `plans_reader_images.py` (new `pdf_chunks_for_pages` added beside the existing `pdf_chunks` :429, which stays unchanged), `llm_queue.py` (job and `_JobSpec` with mark and status callables on `facts_status`), `plans_reader_schemas.py` (`SCHEDULE_ITEM`). Migration MB.

Steps: 1 contracts and the 7154 fixture JSON in final schema (day 1, unblocks WS2 to WS8). 2 textlayer. 3 regions, titleblock, scales, roles, category check. 4 notes, legend. 5 schedules. 6 tags. 7 specs. 8 extract, store. 9 MB, job, gate. 10 integration hooks behind flags (T-INT-1: flags off gives byte-identical requests; T-INT-2: with `plans_reader_page_facts_enabled` on, the planned 7154 requests, built at $0 from the backup PDFs, show ledger `legend_pages` containing E0.00 with >= 60 legend entries, the ledger legend text present in every drawing-category sub request, and a facts block after every page each sub request sends; E16 and E18 in 3.5 read this test's output). 11 api, services adapters. 12 corpus and hand-checked fixtures (BLIND: a separate subagent labels renders before the extractor output is seen; hashed and stored in scratch). 13 replay script `scripts/pagefacts_replay_7154.py` asserting planned groups, total input tokens and estimated USD vs the stored run (budget: total planned input for the reading part at or below the stored 940k tokens).

Tests: synthetic PDFs (rotation, CropBox offset, invisible text, SHX, image cover, soft hyphen, large-font sheet numbers, view bubble, legend example, scale conflict); golden-local 7154 (sheet numbers, titles, scales, roles, MECH relabel, legend, 4 panels with keynote 1 on 1EELC 1, 3, 5, 7 and zero other `new`, L4 LOCAL, L5 linear hint, (X) 34 and 39, circuit tokens 74 as WS1-tokenizer regression values, spec selection); keynote exact string "FURNISH AND INSTALL NEW GFCI TYPE CIRCUIT BREAKER WITH ALL REQUIRED MOUNTING HARDWARE. BREAKER MANUFACTURER AND AIC RATING TO MATCH EXISTING."; existing_reused needs positive evidence (both directions, 3 firms other than IMEG); facts job advances the run; same sha256 labeled specifications in run A and electrical_drawings in run B.

Acceptance: 7154 page-fact checks PF1 to PF7, all on the backup PDFs at $0: PF1 sheet numbers 10/10 exact on ELEC (E0.00, E0.01, E0.02, E0.03, E0.04, E1.00, ED2.01, ED3.01, E2.01, E3.01), titles contain the expected words, `primary_ft_per_in` 16 on E1.00 and 4 on ED2.01, ED3.01, E2.01, E3.01, the E0.00 1/8" legend example flagged, sheet size 42x30; PF2 roles: E0.00 includes legend_symbols and general_notes, E0.03 primary schedule, E0.04 includes schedule, E1.00 to E3.01 primary plan, ledger `legend_pages` contains E0.00; PF3 extraction under 3 s for the 18 drawing pages, p90 page under 0.5 s; PF4 MECH: 8/8 sheet numbers M-prefixed, `category_check.action = relabel` to mechanical_drawings, share 1.0; PF5 legend >= 60 entries including REC-DUP-GFI, REC-TAMP, JB, ECONN; PF6 E0.03 4 panels (1EELC, 1HD, 1LH, 1LI) each `parse_confidence >= 0.95`, keynote 1 linked to exactly 1EELC 1, 3, 5, 7 with status new and gfci true, zero other circuits new, E0.04 6 luminaire types with L4 control LOCAL and L5 `linear_hint`, tags ED2.01 (X) 34, ED3.01 (X) 39, E2.01 circuit tokens 74; PF7 SPECS.pdf 34 sections mapped, no Division 26/27/28, ledger note points to E0.01, tier A pages {24-30, 34-36, 54-57, 75-78}, planned spec input <= 58,000 tokens; corpus (66 projects, first 3 drawing PDFs, up to 40 pages): p90 <= 0.5 s per page, zero crashes; sheet number on >= 80% of electrical text pages with precision >= 98% on a blind fixture of >= 150 pages from >= 12 documents; title exact or contained >= 90%; roles F1 >= 0.9; per plan view scale precision >= 98%; circuit-tag recall measured on >= 10 plan sheets from >= 6 firms; panel cell accuracy >= 98% on 10 schedule sheets from >= 6 firms (prototype the panel parser on 3 schedules from firms other than IMEG, the engineer of record on 7154); per-project share of no-text pages reported. Live: $0. Risks: title-block variety, messy tables (validators, never guess), injection through document text (sanitize, data framing).

### WS2 Tool loop runtime and providers (fixes G8; with WS1 E16, E18, E19; enables E1 chat, E11)

Design. `llm_parts.py` neutral parts and `ProviderImageSpec` (OpenAI 30,000 patches; Anthropic long edge <= 2,576 px and <= 3.75 MP, about 4,784 tokens per tile at the limit, <= 90 images and 30 MB base64 per request). `llm.complete_turn(feature, ModelRequest) -> ModelTurn`; `complete_parts` becomes a wrapper with byte-identical kwargs (snapshot test). `llm_anthropic.py` transport (streaming, adaptive thinking, explicit effort, strict tools sorted by name, structured output, at most 4 cache breakpoints, thinking blocks replayed unchanged, all tool results of a round in one user message, never forced tool_choice, never `budget_tokens`, refusal and max_tokens handled as below). Both transports run inside `_guarded` and renew the lease before sending. `LlmUsage` gains cache-write fields; `llm_pricing` rows for `claude-opus-5-5` ($4 in, $0.20 cache read, $20 out, writes 1.25x for 5 min and 2x for 1 h), `claude-sonnet-5-5` ($2, $0.20, $10), `claude-opus-5` ($5, $0.50, $25, the splitter model); re-verify on build day; Haiku rows deferred. Conservative pricing (most expensive row of the provider) only where `plans_reader_calls` rows are written, never in `llm._log`. Loop (`plans_reader_toolloop.run_loop`): durable rounds, orphan settlement (bill at estimate once, re-send once), idempotent tool calls, write tools keyed `group_id:call_id`, lease renewal around every tool, resume from stored rounds. Budget rules fixed per critique: each round's `max_tokens = clamp((max_usd - spent - reserved_final - input_cost) / output_price)` and the estimate uses that cap; a `max_tokens` stop on a tool round becomes forced-final with the remaining budget, not a permanent failure; the cache gate applies from round 3 using round 2's observed ratio (round 2 predicted as prefix / (prefix + new)); budgets keyed on group_id or message_id. `ai_spend.gate_round` = group budget, run ceiling (`factor x estimated_cost_usd_max`, which `create_run` writes including loop budgets; chat and modify excluded; a hit finalizes or fails the group with a clear message, never the daily-cap pause), daily cap, eval ledger. Kind budgets: recount 6 rounds, 16 tool calls, 8 images, $0.80; main (consolidator) 20 rounds, $6.00 cap; review 10 rounds, $1.50; revise 6 rounds, $1.00. Sub and escalation stay single call (escalation v2 and sub tools deferred). Chat keeps its current `_tool_loop` on `complete_parts` in this build; registry tools are added through an adapter; existing 38 chat tests keep their assertions. Routes are code and env config (`plans_reader_routes`) snapshotted per run; the Settings card and loop UI are phase 2 (a read-only `GET /ai-estimating/runs/{run_id}/groups/{group_id}/loop`, pricing fields redacted, is in). `run_group` gains per-kind builders, systems, schemas, finalize, and allowed statuses; `advance` and `PIPELINE_KINDS` updates (section 4.2) are WS2's integration edits done by the integrator. zoom_region: targets from anchor, expect_text or census; containment check; retarget to union (area <= 3x) else to targets; clusters when far apart; pads by purpose; ppi by purpose stepping down until <= 4 tiles else a $0 error; text_lines; coverage stamped and enforced in the patch merge (incomplete coverage can never replace a sheet total).

Files: `llm.py` (`complete_turn`, route resolution replacing the `openai_only` pin for plans features, the new feature keys of section 4.2), `llm_parts.py`, `llm_anthropic.py`, `llm_pricing.py`, `ai_spend.py`, `plans_reader_tools.py`, `plans_reader_toolloop.py`, `plans_reader_model_routes.py`, run_group edits, `scripts/plans_reader_loop_replay.py`, `scripts/plans_reader_ab.py`; `pyproject.toml` adds jsonschema. Part of migration MC.

Steps: 1 verify the anthropic SDK accepts `output_config`, top-level `cache_control`, tool `strict` (bump within 0.x or pass via extra_body). 2 parts. 3 usage and pricing. 4 complete_turn. 5 Anthropic transport. 6 budgets. 7 MC (integrator). 8 registry, validation, Protocols, fakes (day 1 with steps 2 and 3). 9 tools (zoom on WS1 fixtures). 10 loop. 11 routes. 12 integration. 13 replay and A/B harness. 14 live L1, L2.

Tests: transport fakes (both providers, same scripted policy gives the same tool trace), budget tests (round 1 with 0 cached tokens still allows round 2; max_tokens mid-loop forces final; no group over budget by > 15%), kill-and-resume at 20 random points (zero duplicate writes, at most one duplicate call per crash), E19 retarget to the L4 row with LOCAL in text_lines, E11 coverage widening to all "(X)" tags (assert coverage ratios, not absolute counts), rotated-page zoom, census unavailable on a vector_text fixture, read_text without target returns candidates at $0, pricing tools hidden from the Accountant.

Acceptance: suites green; L1 conformance (tool round and structured final on opus-5-5 and sonnet-5-5, round 2 shows cached tokens, cost equals hand calculation); L2 decision recorded. Live $29: L1 $3; L2 recount route A/B gpt-6-astra vs claude-opus-5-5 on 12 adjudicated count and read tasks (6 from 7154: ED3.01 troffers 17, ED3.01 demo lighting 49, ED2.01 demo power 35, L4 control LOCAL, E2.01 receptacles 74, GFCI 11; 6 from YMCA and Five Star, taken from their dev bundles of WS0 step 7 with truth from their estimator BOQs) $20, repeats on disagreements $6 (paired comparison on the same tasks; budget-truncated tasks reported separately); no reserve (contingency covers overruns). L3 (sub tools) dropped. Main A/B is WS4's.

### WS3 Scale, measurement, route synthesis (fixes E2, E3, E10, E15, E23, E14 length part, E6 quantity side)

Design. Pure modules `plans_reader_geometry.py` (scales per view from WS1 views with legend and symbol blocks skipped; reduced-print check never auto-scales; vector segments indexed once per page per run), `plans_reader_circuits.py` (consumes WS1 tags, panel schedules and panel locations; conductor grammar), `plans_reader_routes.py`, `plans_reader_measure.py` (derive-stage orchestration, `MeasureService`, tools). Registration: shared-token median solve trying the 4 right-angle rotations (and a mirror check), keep the lowest residual; `identity_frame` = at least 3 identical tokens and at least 50% of the common tokens at identical positions; single-anchor registration (k fixed from the scale ratio, translation from one unique multi-word label) allowed at confidence 0.5, cross-checked against the callout-box rectangle; chains through identity frames (7154: E2.01 -> ED2.01 identity, ED2.01 -> E1.00 single anchor). Tag convention classifier per sheet (tag per device, stacked label, home-run arrow): stacked labels and arrows snap to the nearest device symbol or census point via leaders; home-run-arrow sets use proximity grouping (untagged tier). Collector and home-run errors are reported separately on golden projects. Breaker status vs wiring status are separate: a circuit whose description names new loads or that has tags on new-work plans gets new wiring and a home run whatever the breaker status; only "EXISTING LOAD" circuits with an intercept or extend keynote get a 25 ft tie-in. Conductor sizing (Q15): lighting circuits #12 unless voltage drop on load current exceeds 3%; power home runs from the schedule wire column, else voltage drop with I = load_va / volts when the schedule has load, else 20 A branch circuits capped at #10 (calibrated against the golden #10 share). Equipment-connection tier: per connection from the schedule or counts with a home-run allowance, so dropped rows are always replaced. Linear items: vector rectangle candidates nearest the tag (L5); demolition track anchored on keynote bubbles (WS1 keynote markers) or legend symbol match, else a `linear_in_ea` block finding routed to a human (E15 is "measured or flagged"). Control cable length only (25 ft per device default; WS5 owns inclusion). Quantities are emitted as `M<n>` rows in `_CONDUIT_RE` form (`3/4"C EMT`) so the existing renderer derives accessories (E23). Main never writes branch rows: `MAIN_SYSTEM` lines 363-365 become "Branch wire and conduit rows come from the app's measure output (M rows); never write them; never state that scale or calibration is unavailable when the measure output gives scales." A failed derive group never blocks: main runs and the density tier (devices x band median) applies with `branch_density_only` warn. Derive dispatch happens before the cap and builder checks; `_touch` and `renew_lease` between sheets; scale and viewport data in `page_meta`, words stay in WS1 blobs.

Files: the four modules, `plans_reader_measure_bands.py` (mined constant), scripts `plans_reader_measure_eval.py` (`--survey`, `--calibrate`), `plans_reader_markup_truth.py`, `plans_reader_measure_bands.py`. Schema edits per section 5.4.

Steps: B1 geometry on WS1. B2 survey (tagged, untagged_text, vector_text, scanned; tag convention per sheet). B3 circuits. B4 registration with rotation and the 7154 chain test. B5 routes; then RE-BASELINE: run the specified algorithm end to end on the 7154 fixture and record the numbers before judging (prototype constants are not acceptance). B6 linear items. B7 measured runs and markup truth (the markup population of section 8.7). B8 derive wiring (integrator). B9 bands and calibration on the tagged training split only (section 8.7) (grid reduced: route_factor 0.90 to 1.30, drop 0 or 4 or 8, panel_rise fixed 0), LOPO error reported. B10 MeasureService and tools. B11 assist behind the flag. B12 golden scales draft labeled by an AI subagent for 20 projects, owner spot-check optional.

Tests: scale parser cases (1/4" = 4.0; 1/16" = 16; 3/64"; 1-1/2" = 0.6667; engineering; ratio only with a scale word; timestamps rejected; NTS variants); vd_upsize inputs consistent with the formula; registration rotations; tie-in only with keynote; equipment tier; apply_measure_to_boq replaces only covered rows and emits a finding per deleted model row; renderer test for 6,337.71 FT -> 140/698/698/349/349/70.

Acceptance (7154 fixture, zero model calls): EMT within 10% of 6,337.71 FT pre-waste; #12 and #10 within 20% each (provisional; tightened only after B5 re-baseline with owner sign-off); tag counts 74 on 24 circuits and 44 on 3 circuits; scales 4.0 and 16.0, E0.02 NTS with zero lengths; L5 within 5% of 43.46 FT and unit FT; track within 10% of 50.07 FT, measured from the keynote-anchored or legend-matched geometry (on 7154 the human-routed block finding is a FAIL of E15; on other projects it is the correct fallback); control cable 125 FT for 5 thermostats when WS5 includes them. Golden (tagged training projects, n reported, provisional until n >= 15): branch conduit within 15% on >= 60% and within 30% on >= 85%; branch wire within 20% on >= 60%; untagged projects never empty; markup truth within 5% on >= 80% of unambiguous pages; derive p90 < 60 s per 25-sheet set. Live $6 (assist picks on 6 golden pool projects). Risks: tag conventions vary, label positions are not panel symbols (calibration and assist), half-size sets, double counting across enlarged and overall plans (registration dedupe).

### WS4 Consolidation, provenance, integrity gates (fixes E11 containment, E19 regions, E22, E3 gate, E7 enforcement; detects E2, E10, E15)

Design. `plans_reader_rows.py` (ids, fingerprints, coverage per section 5.4; coverage rules: sub rows `sheet_total` unless notes or location match `SUBTOTAL_RE`; escalation and recount rows `sheet_total` only with model coverage whole_sheet AND complete containment; scanned pages use the 0.98 tile-coverage rule; legacy patches without coverage are `inside_tiles_only`). `plans_reader_merge.py`: dedupe ONLY true duplicates (same page in different groups, same normalized full description or similarity >= 0.9, same tag); never dedupe rows from the same group; never sum across pages; patches target by `supersedes_row_id`, else exact normalized description on the sheet; no raise means the target is unchanged and the patch goes to alternates; a raise sets coverage partial and `needs_recount`; conservation invariant test-enforced (all 54 7154 sub rows stay active). `plans_reader_regions.py` (text anchors for schedule rows, tag clusters for counts, WS1 words and views; overall vs enlarged views deduped). `plans_reader_floors.py`: families `demo_x`, `relocate_r` (strong), `circuit` (number optional, each token once, weak and warn-only until the golden check passes), `fixture_mark`, `device_mod` (weak); accounting counts INSTANCES (linear-row segments and superseded-by-measure rows contribute); "none found" is unknown. `plans_reader_lines.py` (code computes quantity, wastage, sheet_refs, basis; consumption at most once per row and dimension). Pre-gates in reading produce recount targets (WS7 module) and nothing else. Consolidator `plans_reader_consolidator.py` on WS2's runtime, first parts 15k to 30k tokens (ledger text, sheets, facts F*, floors, scope digest, measure summary, `propose_lines` candidates including M rows and WS5 breaker rows); strict tool schemas (update fields enumerated as nullable properties); `finish` runs final gates up to 3 times; facts come from WS5 (`facts_for_run`), refusal `excluded_by_fact` only for verified facts and never for raceway-by-EC lines ("FA devices by others; EC provides conduit and boxes" keeps rough-in); `account_rows` dispositions include `existing_circuit` tied to a cited note. Final gates: section 5.4 ids; `new_devices_no_branch` counts cable (MC, AC, NM) as branch and is satisfied by a verified existing-circuit fact or disposition; routes `model_fix` or `human` only. Legacy evaluation mode: gates needing v2 provenance return `not_applicable`; census accounting by BOQ EA sums filtered by sheet_refs; partial_basis by `SUBTOTAL_RE` on material_note and caveat plus merged-row coverage matched by description. Linear class for luminaires per section 5.1 (block only classes >= 80% FT in golden estimates; warn otherwise). Import harness `scripts/plans_reader_import_run.py` (dev run from stored outputs, starts at consolidating, escalations and recounts off for prompt iterations).

Files: `electrical_items` consumers, the six modules, `plans_reader_consolidator.py`, gate persistence, checks routes (`GET /runs/{id}/checks`, override, recount request), `ChecksPanel.tsx` (component only; WS8 owns the tab list).

Steps: 0 minimal import harness `scripts/plans_reader_import_run.py` (wave 1): creates a dev run from stored outputs (the weekend bundle's category outputs, documents and page_meta) at status `consolidating`, using only the existing 0152 tables, and uploads the backup source PDFs to the run's storage so later stages can read pages; flags `--bundle`, `--escalations off`, `--recounts off`; prints the run id. 1 fixtures (run_7154 trimmed JSON, Q27); rows; merge and `_category_output` wiring (roll-up behind the flag); regions and escalation schema; floors; lines; offline golden checks (floors vs estimator totals, demolition two-line style share, which sets the dev value of `plans_reader_demo_two_line_style` by the 5.7 rule, linear FT vs EA share); gates incl. legacy mode; findings persistence including `record_findings(sb, run, findings, stage)` for the WS6 pricing stage; consolidator and scripted-model replay; extend the import harness for v2 rows; live iterations; routes and component.

Tests: conservation on 7154; same-group rows never deduped; patch tile subtotal never total; regions contain 39/39 and 34/34 (model tiles 22/39, 16/34); L4 region contains LOCAL; floors 34 and 39; facts damper is not fire alarm; lines refusals; parse_conductors; never-lower property test; `test_gates_legacy_7154` with the EXACT expected finding set: new_devices_no_branch (146 EA, 0 FT), linear_in_ea x2 (L5, track), partial_basis on the ED3.01 rectangular fixture row, census_floor ED3.01 (34 accounted with track instances excluded per legacy mode vs 39), capability_denial (the E3 sentences), excluded_system moves the Honeywell allowance; nothing else blocks; ED2.01 passes.

Acceptance: deterministic suites; offline floor check estimator_total >= floor - tol in >= 95% of (project, family) pairs else the family is warn-only; live on the imported 7154: 100% of BOQ lines reproduce from sources, 0 orphan rows, 0 unsourced lines, Honeywell allowance absent, branch lines sourced to M rows, consolidation cost <= $3.50 and <= 10 minutes. Live $26: 4 prompt iterations x $3 = $12 (cassette `record_missing`, so unchanged prompts cost $0), main A/B gpt-6-astra vs claude-opus-5-5 on 7154, YMCA, Five Star frozen category outputs $14; no reserve. Risks: tool loop cost on 300+ row sets (propose_lines, cursors, caps, legacy fallback), schema ripple (v2 only in assembly).

### WS5 Scope policy and bid conditions (fixes E4, E5, E6, E7, E8, E12, E14 inclusion, E20, E21)

Design. Versioned policy (`ai_scope_policies`, code seed `scope_policy_seed.py` with evidence from the corpus: 43/43 sent proposals carry the temp-power exclusion, the template exclusion sentence, "Any other scope not enlisted above to be performed by others." and "Add 3% if Payment and Performance bond is required."; 999 of 1,002 estimator LS rows are $0). Engines: `bid_conditions` (project record wins; weighted document cues: NRS 338 including the quoted-abbreviation form `N\.?R\.?S\.?[^0-9\n]{0,20}338\b` and "Chapter 338" 3, Davis-Bacon 3, wage determination 3, labor commissioner 2, public works project or contract 2, prevailing wage 2, certified payroll 1, "not subject to prevailing" -3; owner type; default PW with needs_review; `bond_required` from bid instructions; phasing and after-hours language sets `labor_time: mixed` plus needs_review instead of a drop), `scope_attribution` (document roles, obligations, deterministic attribution, capped LLM fallback for uncertain items; facts F* with WS4's verification rule: the quote must match the cited page TEXT, else `verified=false`; system lexicon with fire_alarm never matching fire/smoke damper; scope split devices_by_others vs raceway_by_ec). Party rule: obligations in owner or GC exhibits that are NOT drawn on electrical sheets are excluded with a clarification; items drawn or counted on E-sheets stay unless an explicit third party (owner vendor, named vendor, other trade, "by others") claims them; drawn-versus-fact conflicts go to `needs_review` (`fact_conflict`); "Division NN NOT USED" never excludes drawn items by itself. `scope_standards`: precedence addendum > keynote > sheet note > schedule note > general note > spec > owner exhibit > house; code-reference sentences ("follow NEC 358") are NEUTRAL; when documents conflict on raceway, resolve to the house value (3/4" EMT, set-screw steel) with a clarification and `needs_review: raceway_conflict`; demolition keynote "remove to source" beats sheet-note "maintain and cap" (E20). `scope_breakers` consumes WS1 PanelSchedule (single parser): keyed NEW rows are breakers to buy; existing-panel positions with new loads and no key are counted into `needs_review: breaker_reuse_assumed` with the count in the clarification; when a KEY is set but its text cannot be resolved, keep the model's breaker rows for those circuits plus a warning. `scope_devices`: merge unpriced modifiers (EMERGENCY, ESSENTIAL, NORMAL, LIFE SAFETY, DEDICATED, colors) in the receptacle family only, conserving totals; TR quantity from legend symbol counts (WS1 legend REC-TAMP), the room rule only writes a clarification; hospital grade and 20A spec grade go to material_note and to `SpecFact`s for WS6. Inclusions are replace-or-merge: canonicalize matching rows (connection family regex) and insert only when no row of the trigger family exists; thermostat triggers deduped across demolition and new-work sheets (new-work sheet wins). Exclusion rules gain `exempt_subgroups`, `exempt_patterns`, `patch(?!\s*(panel|cord))`; hard rules may not carry `unless` (validation). `enforce` runs inside `_finish_with_main` (section 4.1 order), records drops into `excluded_lines` (WS4 conservation), rewrites compound RFIs by removing decided clauses, and reconciles excluded items with markups and marked-plan legends. Overrides apply only their own decision delta to the latest specs; rows added through chat or modify are exempt from automatic rules. Hooks for others: `classify_obligation`, `rules_text`, `evaluate`, `get_scope_policy`. v1 UI: Scope tab (decisions with Include and Exclude) and a JSON policy editor with validate, preview on a run, activate; per-rule editor tables deferred. Proposal lines stored as suggestions only.

Files: `scope_policy.py`, `scope_policy_seed.py`, `bid_conditions.py`, `scope_attribution.py`, `scope_standards.py`, `scope_breakers.py`, `scope_devices.py`, `scope_engine.py`, `scope_tools.py` (registers `get_scope_policy` and `get_facts` in the WS2 registry), `routers/scope_policy.py`, `scripts/mine_scope_policy.py`, `scripts/scope_policy_eval.py`, FE `ScopePanel.tsx`, `ScopePolicyEditor.tsx` (JSON). `build_ledger` selects `wage_type, owner_type, labor_time, labor_needed, project_type`. MAIN_SYSTEM: line 362 and 375 replaced (breakers only for new panels and keyed positions; extra allowances only with a document dollar amount) plus "G3 SCOPE DECISIONS are binding". Migration MD.

Steps: policy model and seed; MD; bid conditions with corpus eval (owner_type hidden variant too); attribution with labeled fixtures (>= 120 statements from >= 8 projects, labels frozen before classifier work, owner spot-checks about 20); standards; breakers on WS1 panels; devices; enforce (7154 replay on REAL parsed text from WS1, not hand fixtures); prepare and digest; wiring; LLM fallback; routes; FE; replay eval over stored runs. Mining and tuning call `excluded_projects("scope_policy")`.

Tests: idempotence and conservation of enforce; RGS-required spec plus a NEC 358 sheet note resolves to RGS or needs_review, never EMT silently; 7154 breakers exactly one row of 4 GFCI; connections stay 7 (no double insert); thermostats 5 (not 10); compound RFI rewritten; overrides keep chat edits; role matrix with `AI_ESTIMATING_STATE=live` patched.

Acceptance: wage agreement >= 93% on the wage population of section 8.7 (71 of 76 when designed; 77 rows today) with owner_type hidden reported separately, cue precision 100%; 7154 replay: extra allowances 10 -> 0, FA, access, telemetry, Centrak, WAP, furniture, door hardware and paint rows 0, each with an exclusion or clarification citing Exhibit A, breaker rows exactly 4 GFCI, thermostats 5 EA and control cable present, raceway EMT with clarification and no raceway RFI, demolition remove-to-source with no retain/remove RFI, receptacles total 74 with no style splits, TR duplex 3 EA from REC-TAMP legend symbol counts, no RFI whose topics are all decided, wage PW; FALSE-DROP metric on golden estimator BOQs fed through enforce: rows dropped or replaced that match an estimator row <= 1%. Live $6: attribution fallback on 8 projects $3, one main-only digest re-run on 7154 through the import harness $3 (provider A/B dropped; no reserve). Risks: over-exclusion (soft rules, Include button, needs_review), regex false positives (word-bounded, corpus test < 1%).

### WS6 Pricing engine (fixes E1, E9, E13, the Verify leak; pricing side of E10, E15)

Design. `electrical_items.py` (section 5.1). `estimate_parse.py` (pure openpyxl; header by "UNIT LABOR HOURS"; columns D base qty, E wastage, F qty, G unit, H unit hours, I unit material, N unit cost, O total; labor rate from the LABOR RATE row; recap per section; all workbooks of the estimate population of section 8.7 (76 workbooks of 68 projects on 2026-10-21) parse or each failure explained). `cost_library.py` (ingest to `cost_sources`, `cost_items`, `cost_observations`; each project contributes one value per item; labor median across projects; material recency-weighted median, half-life 180 days; outlier exclusion; demolition material 0; matching ladder exact -> compatible -> context -> seed -> fallback -> none with strict UoM equality (never EA vs FT, never LS); quality labels; loads page through PostgREST in 1,000-row pages; built with `excluded_projects("pricing_library", case_id)` so live sweeps and LOPO are leave-one-out; holdout always excluded). `pricing_policy.py` (rates PW $125 and non-PW $100 with per-GC overrides; uplift calibrated against ENGINE basis hours on the training pool only, clean band 0.95 to 1.6; `nice_ceil` steps [[200,10],[1000,25],[2000,50],[5000,100],[10000,500],[null,1000]]; tax 8.375% on material only; markup 10% on tax-inclusive material per section; grade seeds keyed on both the HG item_key and the base key). The pure functions of `routers/pricing.py` (`apply_tax`, `tax_info`, `section_summary`, `VERIFY_NUMBERS`) move into `app/services/pricing_formula.py`, imported by both the router and the engine, so verify-replay tests the real code. `pricing_context_from_scope` adapts WS5 output: wage_type from BidConditions, SpecFacts from material_note grade modifiers and scope decisions with their citations as `source_ref`, allowances from the policy (nine standard rows at $0 unless a document names an amount). `vendor_quotes.py`: pypdfium2 text parser (QTY window to SUBTOTAL, item regex, FEET continuation sets FT, notes classifier for tax, EM batteries, controls, commissioning, lot priced, exact-duplicate collapse with warning), LLM fallback behind its flag only for scanned or unparseable quotes, quotes extracted INSIDE the `ai_pricing` job (no separate job type). Reconciliation per category kind: Lighting by fixture marks with EM folding (control rows not in the quote keep library material, flag `not_in_quote`); Switchgear by equipment designations from the BOQ and panel schedules, else manual-eligible flag; subcontract categories (Trenching, Low Voltage) by policy (`replaces: material | material_and_labor | adds`), conduit and wire stay in General Material unless a quote explicitly covers them. `uom_conflict_quote` blocks the row (route back to measurement) and is recorded through WS4 `record_findings(..., stage="pricing")` (section 4.1). Selection: human-selected quote if eligible, else lowest eligible (Q9). Engine `pricing_engine.compute_estimate` pre-gates: `pricing_allowed` from WS4 (optional input re-applied as a status override), `uom_conflict` only for wire, cable, conduit, trench and track rail in EA (fixtures by evidence, section 5.1), `wage_type_unknown`; the "no branch wiring" check is a warning (WS4 owns the blocking gate). Missing rows are priced at a family or context fallback (quality weak, flag `fallback_price`) INSIDE the totals, and the Verify draft also reports `unpriced` hours and material; status `provisional` when a quote-required category (Lighting, Switchgear) has no eligible quote, when fallback-priced share exceeds 10% of hours or material, or when wage source is default. Out-of-band detector (labor-only, gear install without devices, quote-only categories) sets provisional with a reason. Every non-quoted material line lands in a category pre-tax by construction (assertion), which closes the leak. `price_run` runs as llm_jobs type `ai_pricing` claimed in `_claim_plans` (its own small capacity, gated by `plans_reader_enabled` and `ai_pricing_enabled`, never by RFP flags), with a `_JobSpec` (target run id, lease renewal, idempotent version insert), auto-enqueued at step 6 of `_finish_with_main` and on every new BOQ deliverable version (stale flag on older pricing). `pricing_render.py`: the AI estimate template with N4 = the Verify rate, H and I values, J to O and P to R formulas, recap rate cells addressed from the rendered layout (`_write_recap` returns them), adjustment rows (hour round-up, tax, markup) so recap BASE BID equals `verify.bid_total` to the cent, a Verify Summary sheet, and WS8's Basis and Checks sheets (WS8 supplies `basis_rows` and `checks_rows`). Banner "INTERNAL - AI PRICED DRAFT - NOT VERIFIED" until signed. Router `app/routers/ai_pricing.py` prefix `/ai-estimating/pricing` (deps `require_plans_reader`, `require_ai_estimating`, `require_pricing_view`): POST runs/{id}/price (writers; 409 while a job runs), GET runs/{id}, GET versions/{v}/lines, GET versions/{v}/xlsx (`file_access` source `ai_pricing_estimate`), POST and GET runs/{id}/quotes (run-private storage, source `ai_pricing_quote`), GET lookup, GET and PUT settings (PUT VERIFY_ROLES, audited); WS8 adds POST runs/{id}/signoff to this router. Chat: `price_lookup` and `get_price_summary` through the WS2 adapter when the sender is a pricing role and `ai_pricing_chat_tools_enabled`; reader prompts keep `_NO_PRICING` reworded ("never write prices; the engine prices from your quantities"). Deferred: Verify prefill into human tables, LLM item mapper, PricingSettings UI beyond a JSON form, continuous ingest of new sealed workbooks.

Files: `electrical_items.py`, `estimate_parse.py`, `cost_library.py`, `pricing_policy.py`, `pricing_formula.py`, `pricing_types.py`, `vendor_quotes.py`, `pricing_engine.py`, `pricing_render.py`, `pricing_tools.py`, `routers/ai_pricing.py`, `scripts/cost_library_backfill.py`, `scripts/pricing_eval.py` (modes lopo, calibrate, verify-replay, counterfactual, quotes), `scripts/gm_leak_audit.py`; FE `lib/aiPricing.ts`, `PricingTab.tsx` (inside WS8's tab shell), `PricingLinesGrid.tsx`, `QuoteReconcilePanel.tsx`. Migration ME.

Steps: items module completed (S3 moved the names; golden tests unchanged); parser dry run over the estimate population; library and LOPO as pure in-memory code over the backup files (wave 1, no tables); ME; backfill into the ME tables (dev only, from the local backup; wave 2); calibration; formula and engine (`verify_totals` first); quotes; render with recalc check in the `bdr-gotenberg` container; router, roles import, job type, autotrigger; chat tools; FE; end to end on the imported 7154 run with both quotes.

Tests (synthetic numbers only in the repo): verify formula to the cent (per-category rounding), OTHER ITEMS always carried, UoM equality and hard rejects, grade seed only with a fact, quote parse duplicates and FEET continuation, multi-label recap addresses with 3 and 9 labels, job claimed with `RFP_INGESTION_ENABLED=false`, Accountant gets no pricing (routes, chat, thread reads), estimator 403.

Acceptance: verify-replay reproduces 7154 at exactly $117,459.16 and >= 90% of committed verifications within $1 (others explained); General Material pre-tax with estimator units equals $19,054.15; PG-A2 (estimator quantities, library LOPO units, quotes, 625 h at $125) within 3% of $121,760.63 (ADJ4); PG-A3 (engine quantities from the run, engine hours policy, everything priced by the engine) reported, not gated; LOPO on the non-holdout golden pool: strong or good covers >= 70% of basis hours (median per project), workbook-basis hours within 10% on >= 65% with median abs error <= 8%, false-confident rate (section 3.6) <= 5%, zero hard-reject violations; golden Verify band (clean set, recorded rate and markup, selected quotes for non-GM categories and engine material for GM) median <= 8% with >= 60% within 10%, also reported on all projects; 7154 quotes parse ($17,362.00 and $17,550.00, L3 collapse warning, L5 50 FT, L1 21 and L3 16 after folding, `em_premium_check`), the stored AI BOQ (L5 7 EA) blocks with `uom_conflict_quote`; E13 seeds apply only with a cited fact; E1 chat answers hours from the engine for pricing roles; priced xlsx recalc BASE BID equals bid_total. Live $8: quote fallback $4 (only scanned or unparseable quotes reach it), chat checks $4; no reserve. Risks: reviewer uplift uncertainty (range, provisional), placeholder fixture material without quotes (provisional), AI wording lowers coverage (fallback prices, reported).

### WS7 Independent review and blind recount (fixes E11 with WS4; catches E2, E4, E5, E7, E8, E10, E12 to E15, E19 to E22)

Design. History bands (`plans_reader_bands.py`, `ai_history_bands`): metrics on the estimator basis from WS6 `estimate_parse` with the one-book rule (latest Rev book, else most lines), budget and ROM workbooks and labor-only books excluded by a documented filter, devices >= 20 for per-device metrics; building stratum (the bands population of section 8.7; n 29 when designed): hours per device p10 1.02 / p50 2.94 / p90 5.12; branch wire FT per device 22.3 / 45.6 / 94.9 (7154 truth 130.2, above p90); EMT FT per device 6.4 / 15.3 / 27.1; wire per 3/4" EMT 2.96 / 3.08 / 5.24; bid per device $256 / $803 / $1,501; advisory outside p10 to p90, critical only outside [p10/2, 2 x p90] for structural zeros and the total-level metrics whose LOPO baseline (computed in step 1, before any spend) supports it; LOPO bands in eval and live sweeps. Rule lane ($0) consumes WS4 gate findings and WS5 decisions as typed inputs (local rule copies only behind `plans_reader_review_local_rules_fallback`, false in code and dev) and keeps WS7-unique rules: R1 new devices >= 20 and branch wire 0 (critical); R3 linear in EA only for RUN, ROW, TAPE, CONTINUOUS, per-foot schedule or quote, or WS4 linear-class rows (heads, end caps and fixed lengths excluded; warning unless WS4 `linear_in_ea` also fired); R12 unit price outlier only on quote or manual priced lines; R13 duplicates; R14 false RFIs answered by the text layer; R8 only allowances with amount > 0 or non-template names; R7 wage via WS5 bid conditions. New deterministic checks: measured-quantity floor (branch wire FT vs circuits x a per-circuit length floor from the panel schedules, and an independent code re-run of `synthesize_routes` totals, warn when the BOQ is more than 25% below); Verify-basis invariants on the sent number (rate matches wage type, uplift applied, tax on non-quoted material, every non-quoted material section carried, quote coverage when quotes exist); coverage rule (fixture types, panels or equipment marks with no BOQ line; electrical sheets with zero yield). Recount (`plans_reader_recount.py`, kind recount, runs in READING as pass 3): targets P1 partial evidence (WS4 coverage `tile_subtotal` or `partial`, plus `PARTIAL_RE` on notes, material_note and caveat including "tile only|in (the )?(enlarged )?tile"), P2 census shortfall, P3 low confidence with material dollars, P4 top-dollar counted rows (unit cost from `price_lookup` at $0), max 12 targets and 6 sheets; blinded symbol description; code regions from `for_recount`; tiles planned from the route's ProviderImageSpec (6 to 9 per 42x30 sheet on Anthropic); marks deduped by TILE OWNERSHIP (each tile owns a non-overlapping core; keep a mark only inside its owning core); outcomes: confirmed (within tol), raise (one signal allowed only below max($1,000, 1% of total) of impact; above it a second signal is required: family-specific census, a tiebreak on the other provider, or a second sample at a different tile offset), lower only with two signals, else unresolved (finding to human). A census is a raise signal only when family-specific; otherwise it is a ceiling check, and if the recount used text_search it is not independent. Output: COUNT rows with `supersedes_row_id` and code-computed coverage, re-merged once. Review lane (post-run, change slot held; `begin` called at the end of the ai_pricing job): rules -> review group (fresh context, default claude-opus-5-5 effort medium unless high wins on 7154, read-only tools, max 10 tool calls, $1.50, `REVIEW_SCHEMA` findings with evidence and typed ops; quotes verified against the cited page only, >= 20 normalized chars containing a key term, `difflib` ratio, no new dependency) -> revise group (builder route, one turn, `REVISE_SCHEMA` dispositions, $1.00) -> code applies ops with evidence rules (decreases need two signals or a policy hit; additions need a verified quote, recount raise, census floor or a measurement tool result; a unit change never applies without a measured or quoted quantity: the row stays and the finding routes to human) through the modify path (BOQ v+1 rendered, "Revised after independent review") -> re-price -> recheck ($0). Severity calibration applies to reviewer findings only; rule and band severities are fixed; impact None never demotes. A human edit after review marks the verdict `stale`. A pricing job triggered by a review revision carries the instruction marker `review_revision` and never starts another review (one review per base pricing version). Shadow mode computes and shows without writing. Ops use `line_id`. Gauntlet packs use text-layer fallbacks only (page text, spec TOC, legend by search) and are built ONLY from bundles that exist before the core sweep: the 7154 weekend bundle, the 7154 integration and noise bundles, the YMCA and Five Star smoke bundles, and the older YMCA and Five Star dev-run bundles (WS0 step 7); up to 8 bundles, fewer if fewer exist (the budget is a cap). Clean controls and seeded defect variants come from those bundles by replay. Reviewer prompts, thresholds and severity calibration FREEZE when the gauntlet ends, before the core sweep opens; core and holdout bundles may be re-scored by the gauntlet afterwards for the report only, never for tuning.

Files: `plans_reader_bands.py`, `plans_reader_review_rules.py`, `plans_reader_recount.py`, `plans_reader_reviewer.py`, `plans_reader_revise.py`, `plans_reader_ops.py`, `plans_reader_review.py`, `plans_reader_review_schemas.py`, `scripts/build_history_bands.py`, `scripts/review_gauntlet.py`; FE `ReviewPanel.tsx` (component). Migration MF.

Steps: bands with LOPO baseline, file-based in wave 1 (`EVAL_ROOT/bands/<band_version>.json`; PG-A3 restated against it), persisted to `ai_history_bands` after MF in wave 2; rules on 7154 replay; recount module with scripted-model tests (tile ownership, raise and lower rules); live recount on ED3.01; reviewer and revise with scripted tests; post-run chain wiring (integrator); gauntlet.

Tests: the legitimate EA linear-ish rows of the R3 population (section 8.7; 147 rows from 28 golden projects when designed) never fire R3 critical; 7154 breakers handled by WS5 (R6 dropped); PARTIAL_RE matches the ED3.01 escalation notes; tile ownership removes overlap duplicates; set_unit without quantity never applies; revise ops reach the BOQ xlsx (quantities equal priced v+1).

Acceptance: 7154 recount ED3.01 troffers 17 (adjudicated) and demo lighting within 10% of 49; raise precision >= 90% on adjudicated targets; reviewer catches the seeded E-class defects in >= 70% of variants with <= 1 false critical per clean control; review cost <= $4 per run. Live $28: recount live and tiebreak $8, reviewer on up to 8 pre-core bundles x up to 2 variants $15, revise $5 (end to end is covered by the integration runs; reviewer provider A/B dropped; recount provider comes from WS2 L2).

### WS8 Evidence, deliverables, UI (fixes E17; displays E1, E9, E10, E15, E25 results; owns roles and the day-1 flag plumbing; each flag's owner is in 5.7)

Design. Day 1: roles, `ai_pricing_allowed`, `require_pricing_view`, `/features.ai_pricing` (section 5.5). Evidence (`plans_reader_evidence.py` via one adapter module `plans_reader_evidence_inputs.py`): anchors from WS1 words and census (displayed frame, SHX words, non-plan views excluded with WS1 views, per-view scale); placement per EA line: method `anchor` only when the anchor text is the line's own tag, `assigned` for family or reading-order allocation (own tone, excluded from the 90% metric), greedy assignment by distance (Hungarian deferred), `snapped`, `registered`, `approx`; unclaimed anchors drawn as "tag not counted" marks; stacked-mark merge only for model-point marks with a threshold in feet from the view scale; polylines from WS3 route segments via derivation terms (wire lines compare against drawn segment length x conductor count; vertical terms listed as not drawn; linear-item rectangles converted to centerlines); chips per section 5. Evidence keyed by (main_version, boq_deliverable_version), upserted, `inputs_hash` skip, heartbeat during build, source bytes capped per sheet. `deliverable_spec` gains sb and run; marked plans and annotated plans rebuild from evidence on modify and rerender with no model call; `CODE_KINDS = ("annotated_plans",)` limited to download, preview and rerender-from-evidence (never modify or chat); the evidence-derived marked-plans spec is renderer-only (never validated against or sent to a model). Annotated Plans.pdf: full-size copy with `/Circle`, `/PolyLine` (dashed for assumed routes), `/FreeText` legend, optional `/Measure` dictionary. Autonomy module and persisted state (section 5.4). Pricing UI shell: tabs evidence, pricing (only when `features.ai_pricing`), checks; `AutonomyBanner`, `AutonomyChecklist`, `BasisChip`, `EvidencePanel` (crop route `GET /ai-estimating/runs/{id}/evidence/crop.png`), `PricingPanel` reading WS6's API, sign-off modal (VERIFY_ROLES), Checks tab composing WS4 `ChecksPanel` and WS7 `ReviewPanel`. Chat: `explain_row`, `humanize` of D/P references, and the conditional pricing clause. Locale tooling: WS8 adds two flags to `scripts/translate_catalog.py` (today it takes only positional locale codes and re-translates the whole catalog): `--only-missing` (translate only keys absent from the target locale) and `--prefix <dotted.key.prefix>` (restrict to keys under it); the run is `translate_catalog.py --only-missing --prefix aiEstimating.` and never rewrites keys outside the prefix; parity and no-dash checks scoped to the `aiEstimating` subtree (the existing catalogs are not at parity and already contain dashes outside it).

Files: `scripts/translate_catalog.py` (`--only-missing`, `--prefix`, `--sweep`), `plans_reader_evidence.py`, `plans_reader_evidence_inputs.py`, `plans_reader_autonomy.py`, `plans_reader_chat_tools.py`, render additions (`render_annotated_plans_pdf`, marked pages from evidence), routes (evidence, evidence line, crop, autonomy, signoff), FE components and `lib/aiEstimating.ts` types, `RunView.tsx` tab list (WS8 owns it). Migration MG.

Steps: flags and roles; MG; translate_catalog flags (tested at $0 with a fake translator); adapter with fakes; anchors; placement and registration; polylines and chips; marked and annotated renderers; routes; autonomy; Basis and Checks sheet rows for WS6; chat tools and redaction tests; FE and locales; evidence replay on 7154.

Acceptance: E3.01 anchors L1 21, L2 1, L3 16, L4 3, X1 2, L5 7; ground-truth mark accuracy at $0: detect the colored dots on the estimator's flattened Marked Plans.pdf for 7154, register them, report mark precision and recall (target >= 0.85 each within 1.5 ft); anchor recall vs estimator fixture quantities on >= 5 golden projects reported; fixtures for a /Rotate 90 page, an SHX-only page, a two-scale sheet; every BOQ line has a quantity chip, every priced line a price chip; annotated PDF opens in PDFium and pypdf with annotation counts equal to evidence counts, 7154 under 6 MB; autonomy on the stored 7154 reports `blocked` with integrity_gates and lengths_produced failing; security matrix (Accountant never receives a money field, including thread reads). Live $5: 12 scripted chat questions on gpt-6-astra $3 (chat stays on gpt-6-astra in this build, section 8.3; no Claude chat run), translation of new keys $2 (run inside a ledger sweep with `--only-missing --prefix aiEstimating.`).

---

## 7. Build order and dependency graph

Setup (integrator):

- S0 Preserve the planning artifacts (do this FIRST, ideally before the build session starts, because a new session's sandbox may not be allowed to read another session's `/private/tmp`, and `/private/tmp` is wiped on reboot). Copy, with a sha256 manifest `EVAL_ROOT/proto/MANIFEST.sha256` (`EVAL_ROOT = /Users/thomasmoore/Desktop/BDR/backups/eval`):
  - `/private/tmp/claude-501/-Users-thomasmoore-Desktop-BDR/54c6869b-ac08-4209-8861-d7d842a45a6e/scratchpad/weekend/` (the 7154 export, 52 MB on 2026-10-21) to `EVAL_ROOT/bundles/7154/ff8fad25/` (its `scripts/` also to `EVAL_ROOT/proto/weekend_scripts/`);
  - `/private/tmp/claude-501/-Users-thomasmoore-Desktop-BDR/54c6869b-ac08-4209-8861-d7d842a45a6e/scratchpad/spec_ws/` (the nine `WS*.md` designs, `out/`, and `scripts/`, which holds `ws0_gold_ids.json`, `ws0_pool_pages.json` and `corpus.json`; they are in `spec_ws/scripts/`, not at the top of `spec_ws/`) to `EVAL_ROOT/proto/spec_ws/` (4.5 MB);
  - `/private/tmp/claude-501/-Users-thomasmoore-Desktop-BDR/54c6869b-ac08-4209-8861-d7d842a45a6e/scratchpad/research/` to `EVAL_ROOT/proto/research/` (648 KB).
  DONE by the planning session on 2026-10-21: all three copied, `EVAL_ROOT/proto/MANIFEST.sha256` lists 314 files (the bundle under `bundles/7154/ff8fad25/` plus `proto/`) and verifies with `cd EVAL_ROOT && shasum -a 256 -c proto/MANIFEST.sha256`. Paths inside this spec that say `spec_ws/...` or `weekend/...` mean these copies. The build session starts by checking that `EVAL_ROOT/proto/MANIFEST.sha256` exists and verifies. If S0 never ran and the scratchpad is gone, these become `skip` (never rebuilt from prod): the weekend baseline values of every 3.5 row and the detector self-tests, the E25 scorer baseline, the Q27 fixtures, the S2 stored-output dev run, the WS4 conservation test on the 54 sub rows and `test_gates_legacy_7154`, the WS8 autonomy test on the stored 7154, the weekend gauntlet bundle and the 7154 regression check of 1.3 item 2. Substitute (owner note, from contingency, about $18): one dev run of 7154 with every new flag off (old pipeline) through `eval_run.py`; its export becomes the baseline bundle and its measured values replace the weekend baselines in 3.5 (record both). The 66 golden ids are also in section 8.1.
- S1 Create the `EVAL_ROOT` layout (`manifest/`, `references/`, `bundles/`, `cassettes/`, `replays/`, `sweeps/`, `reports/`, `bands/`, `proto/`) outside both git repos.
- S2 moved to wave 2 (it needs `eval_run.py`, WS0 step 13, and the minimal import harness, WS4 step 0): (a) `eval_run.py --case 7154 --no-start` creates a dev run with the backup files under the 3.2 label map; (b) `scripts/plans_reader_import_run.py --bundle EVAL_ROOT/bundles/7154/ff8fad25` creates a dev run with the stored weekend outputs at status `consolidating`. Record both run ids in the build log. Wave 1 needs no dev run: it works on files (backup PDFs and the weekend bundle) through replay with `ctx.sb is None`.
- S3 Reserve migration numbers MA to MG; write the day-1 contract skeletons (the roots of the dependency graph below): `estimating_contracts.py` (artifact types `bdr.priced_estimate/1` and `bdr.gate_report/1`, the Check registry, `GateFinding` and the `record_findings(sb, run, findings, stage)` protocol, `Fact` and the `facts_for_run` protocol, `ExcludedLine`, the `pricing_allowed(run) -> (bool, reasons)` signature), `pricing_types.py`, `electrical_items.py` (moved names, section 5.1), `plans_reader_tools.py` (registry, Protocols, `Unavailable` defaults, fakes), `plans_reader_review_schemas.py` (the 5.8 schemas, validated with `validate_strict`), roles and flags (WS8 step 1).

Module dependency graph (edges run from prerequisite to dependent). Every implementation imports only the S3 contracts of its siblings, never a sibling implementation, so each cross-workstream edge is satisfied first by a contract plus a WS2 fake and later by the real module, which the integrator wires in wave 3. This breaks the workstream-level cycles (WS4 and WS5 on facts and excluded lines, WS4 and WS6 on `pricing_allowed` and `uom_conflict_quote`, WS4 and WS7 on recount targets and re-merge, WS3 and WS5 on control cable inclusion).

```
S3 contracts (estimating_contracts, pricing_types, electrical_items, plans_reader_tools, review_schemas, roles, flags)
 |-> WS1 pagefacts --> WS3 geometry, circuits, routes, measure
 |                 --> WS4 regions, floors          --> WS2 registry services (TextIndex, Census, Schedule, Legend)
 |                 --> WS5 breakers, devices, attribution text
 |                 --> WS8 evidence inputs, anchors
 |-> WS2 llm_parts, complete_turn, llm_anthropic, budgets --> WS2 toolloop --> WS7 recount, reviewer, revise
 |                                                                       --> WS4 consolidator
 |                                                                       --> chat adapter (WS2 runtime, WS8 tools)
 |-> WS6 estimate_parse, cost_library --> WS7 bands; WS0 line value v; PG-A2 and PG-A3
 |-> WS0 scorer, replay, acceptance (consume every artifact through the contracts; never imported by product code)

Run-time order on one run (each arrow is a call through a contract):
WS4 rows, merge --> WS4 pre-gates --> WS7 recount --> WS4 re-merge (consolidate_category(prior=...), once)
--> WS5 scope_engine.prepare --> WS3 build_measure (reads WS5 inclusions: thermostats, control cable)
--> WS4 propose_lines + consolidator (reads F* facts and M rows) --> WS5 enforce (writes excluded_lines)
--> WS4 final gates (stage final) --> WS8 evidence --> render --> WS6 price (reads pricing_allowed;
writes uom_conflict_quote with stage pricing) --> WS7 review, revise, re-price --> WS8 autonomy.recompute
```

Waves (parallel inside a wave; the integrator serializes shared-file hooks). Every numbered workstream step is in exactly one wave:

| Wave | Work | Depends on |
|---|---|---|
| 1 | Migration MA on dev (tables only, dev only; the ledger exists before any live call). WS0 steps 1 to 12 (scorer, classifier gate, references, manifest, adjudications, contracts, bundles from files, replay, acceptance with self-tests, MA and ledger and allocations, cassette, prompt hash). WS1 steps 1 to 8 (extraction, offline). WS2 steps 1 to 6 and 8 (parts, usage and pricing rows, complete_turn, Anthropic transport, budgets, registry with fakes). WS4 step 0 (minimal import harness), fixtures, rows. WS5 policy model, seed, bid conditions with corpus eval. WS6 parser dry run, library and LOPO in memory (no tables yet), `pricing_formula` move, `verify_totals`, engine (pure). WS7 bands file-based with the LOPO baseline. WS8 flags, roles, anchors on WS1 fixtures | S0, S1, S3 |
| 2 | Migrations MB to MG on dev (integrator, letter order; MC includes WS2 step 7). S2 seeding. WS0 step 13 (`eval_run.py`). WS1 steps 9 to 13 (MB job and gate, hooks with T-INT-1 and T-INT-2, api and adapters, blind corpus fixtures, 7154 replay script). WS2 steps 9 to 11 (tools, loop, routes). WS3 B1 to B7, B9, B10 (MeasureService and its tools). WS4 merge, regions, escalation schema, floors, lines, offline golden checks (sets the E22 flag value), gates (legacy mode first), findings persistence with `record_findings`. WS5 attribution, standards, breakers (WS1 panels), devices, enforce, `scope_tools.py`, MD. WS6 ME, backfill into tables, quotes, render, router, job type, autotrigger. WS7 rules, recount module (scripted), bands persisted after MF. WS8 MG, `translate_catalog.py` flags, adapter with fakes, placement, polylines, chips | wave 1 contracts and WS1 api |
| 3 | Spine integration (integrator): derive stage (WS3 B8 + WS5 prepare), recount pass 3, consolidator (WS4), `_finish_with_main` order, post-run chain (pricing then review), autonomy recompute; WS2 steps 12 and 13 (integration, replay and A/B harness); WS3 B11 (assist behind its flag) and B12 (golden scales draft); WS4 consolidator with scripted-model replay, extended import harness, checks routes and component; WS5 prepare and digest, wiring, LLM fallback (scripted), routes, FE; WS6 chat tools, FE, end to end on the imported 7154 run with both quotes (code only); WS7 reviewer and revise (scripted), post-run wiring; WS8 marked and annotated renderers, routes, autonomy, Basis and Checks rows, chat tools and redaction, FE and locales, evidence replay; WS0 step 14 (the $0 baseline table, published before wave 3 ends); all $0 replay suites green | waves 1 and 2 |
| 4 | Live, in this order: WS2 L1 conformance; WS2 L2 recount route A/B; WS7 live recount on ED3.01; WS4 consolidator iterations and main A/B (imported 7154); WS3 assist probes; WS5 and WS6 probes; WS8 chat checks and translation; integration 7154; smoke sweep; 7154 noise repeat; WS7 gauntlet on the pre-core bundles, then the reviewer FREEZE; core sweep; holdout sweep (frozen code, clean tree, once) | wave 3 green |

Critical path: WS1 extraction -> WS3 routes and derive -> WS4 consolidator and spine -> integration 7154 -> smoke -> core -> holdout. WS0 must publish the $0 baseline before wave 3 ends so every change is judged against it. Do not start wave 4 with any red deterministic suite.

---

## 8. Evaluation and budget plan

### 8.1 Golden tiers (fixed now; holdout never used for tuning, mining, thresholds, prompts or adjudications)

| Tier | Case | Project id prefix | Type, wage | Big sheets | Spec pp | Sent truth (sends) | Committed Verify |
|---|---|---|---|---|---|---|---|
| smoke | 7154 UMC Outpatient Pharmacy | 7dfc64e6 | hospital TI, PW | 18 | 170 | yes (7) | yes |
| smoke | 7177 YMCA Torrey Pines | ba167b17 | new construction, PW | 9 | 38 + 5 addendum (scanned addendum) | yes (1) | yes |
| smoke | 7172 Five Star Resolution | dc0cb2f0 | TI, non-PW | 25 | 0 | yes (1) | yes |
| core | 7181 Windmill Library Conf Room | 2bf3b21e | TI, PW | 12 | 182 | yes (1) | yes |
| core | 7158 Specialty Veterinary Hospital | 061335de | TI, PW | 14 | 0 | yes (2) | yes |
| core | 7196 Sloan Road | 2b3586cd | roadway, PW | 9 | 293 + 3 | yes (3) | yes |
| core | 7179 Replace Interior Lighting | c45e3f91 | lighting retrofit, PW | 19 | 0 | yes (1) | yes |
| core | 7167 Gaudin Ford CVC Facelift | b9bd3846 | other, non-PW | 13 | 0 | yes (1) | yes |
| holdout | 7188 Cimarron-Memorial HS | 078c470d | other, PW | 1 | 152 | yes (2, two different totals) | yes |
| holdout | 7173 Bob Price Rec Center | ba9f0bc2 | other, PW | 12 | 674 | yes (7) | yes |
| holdout | 7178 UMC MLK Warehouse Remodel | 220937f8 | TI, PW | 14 | 75 + 43 | yes (11) | yes |
| holdout | 7148 Palazzo 1730C Boutique | 346a995e | TI, PW | 20 | 0 | yes (1) | yes |

Big sheets = pages of at least 24 x 18 in in the `drawing`, `electrical_drawing` and `addendum` files (`ws0_pool_pages.json` field `big`); YMCA's 9 are its 7 one-page drawing and electrical PDFs (the memory note's 7) plus 2 large pages of its 5-page addendum (verified 2026-10-21; that addendum's filename names "LVCC Central", so WS0 step 3 confirms it belongs to 7177 before the smoke sweep and records the answer). Sent truth = `proposal_sends` rows with status `sent`; committed Verify = a `verifications` row with `committed_at` set; both verified from the backup on 2026-10-21 for all 12 cases. Where a case's sends differ (7188), truth is the latest `sent_at` and the spread is reported.

Pool (mining and LOPO allowed): the other 54 of the 66 golden projects (drawings + BOQ + estimate in the backup; 43 have specs; 39 have both a committed Verify and a sent proposal). The 66 ids:

```
026b2dc5-676f-4ebd-b9a2-b744238b4049 061335de-ac0c-497f-b1da-b214a5bb112d 078c470d-efa1-479c-b579-1690f5da47a0
083dd5f3-e604-44c6-b299-fd17f4a531f5 0b68e4f9-7950-42fb-abc4-04a451e41236 0d03bc95-407f-47c0-8369-1fbed9c7a4cb
0d81db15-45ab-40cd-a7cf-f029b1783712 114e2536-c3f7-43cd-9eb8-1e88a98a988a 1b21c1c6-d12f-4b31-874e-976450af24e7
1c1eb1fa-b967-41f8-8b89-647ab8a5e32b 220937f8-c69d-43b2-b5f8-b2e50f350310 26ce6d73-e313-432c-a9b4-b5452a0e8b7b
27789224-5aee-43e1-ac6e-9559bc8f9ff1 2b3586cd-0364-4ace-a933-bd3efa353933 2bf3b21e-5ce4-406c-aafe-592dbc7d00e7
301b4da2-0a80-4536-b168-638ffce5d7f8 346a995e-3758-40cc-994c-008e00f299c6 39b6ed51-4d3d-4d82-95a0-ed857b1c0fff
3ae4c335-a2f1-4b27-8460-e3e62f2ca984 45b39974-ba06-48be-b5ac-42cbcfbf7c90 4a382f4b-237d-4a33-be1d-6daa902a2521
4ef93bcb-c114-41da-9892-ac5758cf07be 5982a438-8e91-4440-a359-09bc0a7ee9cb 5e224aab-8637-452b-9d84-c4ed3c353a3c
63f3e354-1434-4b31-ab36-d124bfbcb8aa 6961f40a-4a89-4d98-9382-8e27d4d7466d 6b441d52-c423-476d-ae2f-34e2ec9607b0
7357cc7c-1878-43ce-9d4e-760b31a00a08 7589a596-6785-40a3-bb05-92760c941a52 7dfc64e6-36ef-4c67-9d02-0d7942015011
82b14adb-e8e6-41cd-93af-5f929fa3ad05 84f0771e-597b-41e2-a6ac-91399dbffdbd 8804e10a-a87b-40ab-a4a2-c86390b66002
88319400-db76-4cfc-8c21-f8d254097935 895cd2d5-bf9a-4b50-ac04-aee64a535943 8c08ff60-f9cc-43de-932e-03362f641033
8e4d09c4-8faf-4b5a-9407-3c038612ea84 91c65c7c-d658-4fb8-9c4c-e17efe116fd8 9b772ebf-4ad0-4830-a945-dd91fb81bb1c
9d1de4ea-8293-48d7-9e0d-c7c370cda370 9ee491b9-1821-4d5d-bee0-b1795bcabac5 aec676fa-55b0-4253-83cd-151482ed34fe
b17205ef-0fe8-4994-9c47-092d7a14d6b4 b185155d-c74d-442e-9ea5-04b8cb3fc023 b2211f30-3ffb-4bea-ac8a-24925113134a
b9bd3846-9759-4847-ac1d-19a349c83ab3 ba167b17-0ff5-4012-a638-403bcced09af ba9f0bc2-ef71-4bfe-8b4f-70f92cf0c4b9
c400b8fd-00bd-465d-ba4c-ae602acdf4ed c4428db0-41c1-4345-8471-4c23050cd0c6 c45e3f91-133e-4b1b-bc1b-49e2c8b36f02
cab0a700-bf0e-497c-abc2-75a46c9449bb cd4ee594-220b-4df7-96c1-28ab050070e0 cfeb91fc-691f-4efd-bc8c-312bb7c51a66
d3efbef9-cda3-43f7-8143-fd29822881b1 db9ee88d-bfa5-4ecb-ae16-98bce44865cf dc0cb2f0-bc93-4bb5-9a17-79adf151a5fb
e70b9bdc-ed55-4a87-bc6b-db408489d077 e858f05a-e763-48f2-9f75-5b4bc8e3ffc2 e9ad59d9-d9f3-4d8c-904e-b35bb7f1aed0
f33871cc-b673-42fd-ba1e-36e2c3bf17c9 f9182cfc-978c-4d30-8717-ed5aab09b6c8 f9be8b69-3b1b-44f0-93d8-0f72e07ad937
fb767cc1-80a7-4064-8745-e046445bc8b0 fdd8eb5e-ceff-4369-864a-a2bd6692a140 ff059866-b011-4878-b90f-634b42f4e88e
```

### 8.2 What runs at $0

Scorer and classifier gate; references and data-quality checks; acceptance detector self-tests; replay of stored bundles through every code stage (merge, measure, scope, consolidate-legacy, price, gate, render, score); cassette replays of unchanged prompts; scripted-model tests of every tool loop; WS1 corpus extraction; WS3 survey, calibration, markup truth; WS4 offline floor and style checks; WS5 mining, bid-condition eval, enforcement replay, false-drop metric; WS6 LOPO, calibration, verify-replay, counterfactuals, quote text parsing; WS7 bands and gauntlet re-scoring; WS8 evidence replay and mark ground truth. Adjudications are reviewed by a human (Q1); unreviewed cases run as `unreviewed`.

### 8.3 Model choices and how they are decided

| Step | Default route | Decided by |
|---|---|---|
| sub | openai gpt-6-astra, medium, single call, facts block | fixed (proven counting); sub tools deferred |
| escalation | gpt-6-astra, medium, single call, code regions | fixed |
| recount | claude-opus-5-5 medium until L2; then the L2 winner (exact tasks; ties within 1 task to the cheaper by $ per task) | WS2 L2 |
| main (consolidator) | gpt-6-astra medium | WS4 A/B on 3 cases: final-gate pass rate, matched lines, refused-then-abandoned tool errors, cost; within 2 points the cheaper |
| review | claude-opus-5-5, medium (high only if it wins on 7154), different provider than the builder | fixed rule; same-provider only if a later A/B shows > 10 points better finding precision |
| revise | builder route (gpt-6-astra) | fixed |
| scope attribution fallback | the run's main route model (`plans_reader_routes` step `main`, gpt-6-astra by default), low effort, feature `plans_scope_attrib` | fixed |
| quote fallback (`pricing_quote`), derive assist (`plans_derive`) | gpt-6-astra | fixed; may switch to a cheaper vision model on evidence |
| chat | gpt-6-astra (existing loop) | unchanged in this build (no Claude chat run; WS8 live spend follows this) |

Every live choice and its numbers go in the build log with run ids.

### 8.4 Allocation of the $400 (enforced by the ledger)

Per-case planning estimate (replace with the real `eval_run.py --dry-run` number before each sweep). Formula: reading = $0.90 per big sheet (observed $0.74 on YMCA, $0.79 on 7154 and $1.04 on Five Star per big sheet, sub plus escalation plus roll-up), except 7154's 8 MECH sheets at $0.20 each (relabeled, reduced reading); specs after selection = $0 with none, $1.00 up to 200 pages, $2.50 above; post-reading = consolidation $3.00 + recount $1.00 + review and revise $2.50 = $6.50 (caps $6.00, $2.00 and $4.00). Splitter cost for unlabeled drawing files is not in the formula; the dry run adds it.

| Case | Tier | Big sheets | Spec pp | Reading | Specs | Post | Planning total |
|---|---|---|---|---|---|---|---|
| 7154 | smoke, integration, noise | 18 (10 ELEC + 8 MECH) | 170 | 10.60 | 1.00 | 6.50 | 18.10 |
| 7177 | smoke | 9 | 43 | 8.10 | 1.00 | 6.50 | 15.60 |
| 7172 | smoke | 25 | 0 | 22.50 | 0 | 6.50 | 29.00 |
| 7181 | core | 12 | 182 | 10.80 | 1.00 | 6.50 | 18.30 |
| 7158 | core | 14 | 0 | 12.60 | 0 | 6.50 | 19.10 |
| 7196 | core | 9 | 296 | 8.10 | 2.50 | 6.50 | 17.10 |
| 7179 | core | 19 | 0 | 17.10 | 0 | 6.50 | 23.60 |
| 7167 | core | 13 | 0 | 11.70 | 0 | 6.50 | 18.20 |
| 7188 | holdout | 1 | 152 | 0.90 | 1.00 | 6.50 | 8.40 |
| 7173 | holdout | 12 | 674 | 10.80 | 2.50 | 6.50 | 19.80 |
| 7178 | holdout | 14 | 118 | 12.60 | 1.00 | 6.50 | 20.10 |
| 7148 | holdout | 20 | 0 | 18.00 | 0 | 6.50 | 24.50 |

Sweep totals from the table: smoke (YMCA + Five Star) $44.60; core $96.30; holdout $72.80; 7154 full run $18.10; 7154 re-run from `consolidating` through the import harness (reading reused) about $5.50.

| Allocation | USD | Buys |
|---|---|---|
| WS2 | 29 | L1 $3, L2 $20, L2 repeats $6 |
| WS3 | 6 | assist probes on 6 pool projects |
| WS4 | 26 | consolidator iterations 4 x $3 = $12, main A/B $14 |
| WS5 | 6 | attribution $3, digest re-run $3 |
| WS6 | 8 | quote fallback $4, chat $4 |
| WS7 | 28 | recount live and tiebreak $8, reviewer gauntlet $15, revise $5 |
| WS8 | 5 | chat $3, translation $2 |
| Shared (WS0 runs) | 278 | integration 7154 $45 (up to 2 full runs and 1 re-run from consolidating); 7154 noise repeat $18; smoke YMCA + Five Star $45; core-5 $97; holdout-4 $73 |
| Contingency | 14 | released only by `allocate` with an owner-visible note |
| Total | 400 | |

Probes total $108. The earlier split ($156 probes, $200 shared, $44 contingency) could not pay for the sweeps at the program's own cost figures; Q3 covers this split.

Allowed reductions, in this order, when a sweep's dry run exceeds what remains minus $10 (tiers stay frozen; nothing is ever swapped in from the pool, because pool projects feed mining and tuning): (1) core drops 7179, then 7158 (pre-named; the statistics of 1.3 item 2 are computed on the cases run and the report says which were dropped); (2) the holdout runs with `plans_reader_review_mode=off` (saves about $10; the report says so and the review effect is judged on smoke and core only); (3) stop and ask the owner. After the first integration run, replace the $0.90 per-sheet rate with the measured one, recompute this table and record it.

Checkpoints: after the wave 4 workstream probes, spend <= $108; after integration, noise and smoke, <= $216 cumulative; after core, <= $313; the holdout opens only if its dry-run estimate plus $10 remains. Prior real costs: 7154 $16.56, Five Star $25.99, YMCA $6.65.

### 8.5 Sweep backend

Preflight, every time: (1) the `SUPABASE_URL` check of 0.2 shows `bpidntbyvoooqvaispup`; (2) `lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(5051|5061) '` shows nothing; stop a backend this session started, and ask the owner before stopping one it did not start; (3) on dev, `select claimed_by, job_type, count(*) from llm_jobs where status = 'running' group by 1, 2;` shows no running job (any `claimed_by` is another worker's `_WORKER_TOKEN`); (4) the sweep is open (`scripts/eval_ledger.py open ...`) and its purpose decides `LLM_CASSETTE_MODE` (0.4 rule 6).

Start it as ONE Bash call with `run_in_background: true` (Bash-tool shells keep no variables between calls, and uvicorn never returns):

```
cd /Users/thomasmoore/Desktop/BDR/bdr_be && EVAL_ROOT=/Users/thomasmoore/Desktop/BDR/backups/eval && mkdir -p "$EVAL_ROOT/cassettes" "$EVAL_ROOT/logs" && caffeinate -i -s env EVAL_ROOT="$EVAL_ROOT" PLANS_READER_ENABLED=true FULL_SELF_HOSTED_LLMS_ENABLED=false LLM_QUEUE_ENABLED=true BID_FILE_SPLITTER_ENABLED=true AI_EVAL_LEDGER_ENFORCE=true LLM_CASSETTE_MODE=record LLM_CASSETTE_DIR="$EVAL_ROOT/cassettes" LLM_QUEUE_CLAIM_TYPES=plans_reader_agent,plans_reader_main,plans_reader_chat,plans_reader_modify,plans_reader_facts,ai_pricing,bid_split RFP_INGESTION_ENABLED=false EMAIL_INGEST_ENABLED=false RFQ_POLLING_ENABLED=false DUE_REMINDERS_ENABLED=false DUE_DIGEST_ENABLED=false CALL_IN_ENABLED=false AUTO_SEND_POLLER_ENABLED=false RFP_HARVEST_ENABLED=false PM_RFI_SYNC_ENABLED=false BROWSER_AGENTS_ENABLED=false NOTIFICATION_EMAILS_ENABLED=false LLM_HEALTH_ENABLED=false PLANS_READER_CONCURRENCY=1 uv run uvicorn app.main:app --port 5061 --workers 1 > "$EVAL_ROOT/logs/sweep_backend_$(date +%Y%m%d_%H%M%S).log" 2>&1
```

Then wait for `curl -sf http://127.0.0.1:5061/health` to succeed (use the Monitor tool with an until-loop; a foreground `sleep` is blocked in the Bash tool), and check `/ai-estimating/status` shows the ledger enforced. Change `LLM_CASSETTE_MODE` to `record_missing` for `dev_probe` sweeps. The env names in this command were checked against `app/core/config.py` on 2026-10-21, except `LLM_QUEUE_CLAIM_TYPES`, `AI_EVAL_LEDGER_ENFORCE`, `LLM_CASSETTE_MODE` and `LLM_CASSETTE_DIR`, which WS0 adds (and `plans_reader_facts`, `ai_pricing`, which MC adds to the job-type CHECK); re-check before use. Authenticate as in `bdr-e2e-api-testing.md` (`eval_run.py` reuses `plans_reader_live_run.py`'s `_Auth`). The dev daily cap ($75) still applies; raise it on dev for a sweep with `ai_spend.set_cap` if needed (dev only). Stop the backend when the sweep closes.

### 8.6 Truth sources in the backup

Backup root `/Users/thomasmoore/Desktop/BDR/backups/BDR_Prod_2026-10-01/`: `data/<table>.json` (one JSON array per table), `storage/project-files/<project_id>/<category folder>/<32-hex>-<filename>` (the folder does not always equal the category: map files through `data/project_files.json`), `storage/_objects.tsv`, `schema/`, `manifest.json`. Join key is `project_id` unless stated.

| Truth value | Source (file: keys) | Rule |
|---|---|---|
| Estimator BOQ | `project_files.json` category `boq` -> xlsx; parsed by `parse_estimator_boq` | latest file by `created_at` |
| Estimator estimate lines and recap | category `estimate` -> xlsx; `estimate_parse` (WS6) and `auto_send_estimate.parse_recap` | one-book rule (8.8) |
| Estimator marked plans | category `marked_plans` -> PDF | latest |
| G3 sent total | `proposal_sends.json`: `status = 'sent'`, `labor_amount + material_amount + gear_amount + underground_amount + low_voltage_amount` (nulls as 0), `sent_at`, `gc_id` | latest `sent_at`; spread across GCs reported |
| G3 Verify | `verifications.json`: `committed_at` not null; `labor_amount`, `materials_amount`, `gear_amount`, `underground_amount`, `low_voltage_amount` and the five `*_markup_amount` (the ten `VERIFY_NUMBERS`) | one row per project |
| G3 labor hours and rate | `labor_reviews.json`: `labor_notes` parsed by `Labor Hours = (\d+(\.\d+)?)` and `Labor Rate = \$(\d+)`, `labor_amount` (`labor_breakdown` is empty in the backup) | parses on 38 of 46 when designed; unparsed -> hours unknown |
| Vendor quotes | `quotes.json` (`rfq_id`, `vendor_id`, `amount`, `is_selected`, `tax_included`, `tax_rate`, `quote_file_id`) joined to `rfqs.json` (`id` -> `project_id`, `material_category_id`) and `material_categories.json` (`name`, e.g. Lighting, General Material, Switchgear); PDF via `quote_file_id` -> `project_files.json` | selected quote per category; all eligible kept as alternatives |
| General Material | `general_material_estimates.json`: `amount`, `tax_included`, `tax_rate`, `raw_extraction` | one row per project |
| Wage type, labor time, project and owner type | `projects.json`: `wage_type`, `labor_time`, `project_type`, `owner_type`, `labor_needed` | |
| Project GCs (rate overrides, Q5) | `project_gcs.json`: `project_id`, `gc_id` -> `general_contractors.json` | |

### 8.7 Populations (filters over the backup; the holdout is excluded from every one except where stated)

| Name (used in) | Definition |
|---|---|
| Golden (66) | the ids in 8.1 (`ws0_gold_ids.json`): drawings + BOQ + estimate in the backup |
| LOPO population (1.3 item 4, WS6) | golden ids with a committed Verify and a sent proposal (39), minus the holdout (35 on 2026-10-21), minus cases failing the reconcile filter (Verify sum equals the latest sent total within $1; e70b9bdc and f33871cc failed when designed); report the final n |
| Estimate population (WS6 parser and library) | every `estimate` xlsx in the backup (76 files over 68 projects on 2026-10-21), minus holdout projects for the library; the parser dry run covers all 76 |
| Wage population (WS5, Q13) | `projects.json` rows with `wage_type` set (77 on 2026-10-21, 62 prevailing wage), minus rows whose name starts with `ZZ` or contains `TEST`; holdout included only in the report column "holdout" |
| Markup population (WS3 B7) | projects with `project_files` category `markup` (33 files over 30 projects on 2026-10-21), minus holdout |
| Bands population (WS7) | estimate-population projects, one book each (one-book rule), minus budget, ROM and labor-only books (documented filter), devices >= 20 for per-device metrics; building stratum = `project_type` in ti, other, new_construction, casino_*, multi_family (n 29 when designed); LOPO in eval and live sweeps |
| R3 population (WS7 test) | EA rows in golden estimator BOQs whose description matches `LINEAR\|STRIP\|TAPE\|TRACK\|UNDERCAB\|COVE` and does not match `HEAD\|END CAP\|\d+\s*(FT\|')\b` (147 rows from 28 projects when designed); count recorded |
| Tagged training split (WS3 B9, Q17) | golden pool projects (not core, not holdout; 7154 included) whose WS3 B2 survey class is `tagged`; the split is written to `EVAL_ROOT/manifest/ws3_split.json` before calibration |
| Old-pipeline bundles (1.3 item 2) | 7154 weekend bundle; the 7172 and 7177 dev runs of WS0 step 7 |

### 8.8 Glossary

- Pro Max: the existing chat toggle (PLANS_READER.md 3.9) that lets chat render and look at actual pages (`read_pages`); stored per message as `pro_max`. A tool with `needs_pro_max` is offered only on such messages.
- IMEG: the electrical engineer of record on the 7154 drawings (its name is in the E0.00 title block); "firms other than IMEG" means drawing sets from other engineering firms.
- Verify leak: the G3 process gap found on 7154 where the General Material extraction took only WIRING material and OTHER ITEMS material never reached Verify (section 3.3); GC1 checks the engine never repeats it.
- One-book rule: per project use the latest `Rev` estimate workbook, else the one with the most lines.
- Clean band: projects whose G3 hours over engine-basis hours fall in 0.95 to 1.6; used to calibrate the hours uplift (WS6) and for the golden Verify band.
- Run model: the run's main route model (`plans_reader_routes` step `main`).
- Generic scope score, false-confident rate, dollar coverage, dollar-weighted error, noise floor: section 3.6.
- Old pipeline: today's code with every new flag off.

---

## 9. Release gating

- Everything in this spec stays DEV ONLY until the owner approves a release. Every parent flag is off in code and on only in the dev `.env`; child settings that default true are inert while their parent is off (section 5.7).
- Migrations MA to MG are applied to dev only. MA (the `ai_eval_*` tables) never goes to staging or prod; shipped code never reads those tables outside eval mode. The eval columns that shipped code touches (`plans_reader_runs.eval_case_id`, `eval_sweep_id`, `llm_call_log.eval_sweep_id`) are in MC and ship with it. MB to MG go to staging and prod only at a release with explicit owner approval, after `list_migrations` checks on each target, in letter order, with PostgREST reload.
- Prod stays `AI_ESTIMATING_STATE=test` (Executive and IT Admin), `AI_PRICING_ENABLED` unset, `plans_reader_consolidator_mode=legacy`, `plans_reader_gates_blocking=false`, `plans_reader_review_mode=shadow` or off, `plans_reader_marked_plans_mode=model` until the owner flips them after reviewing the golden report.
- Pricing visibility: internal pricing roles only (section 5.5); the external estimator never reaches AI Estimating (`require_internal`) and the pricing router 404s when the flag is off. Priced files are never stored in `project_files` and never listed in the estimator portal.
- Anthropic routes for plans steps in prod need the owner's answer to Q22.
- Nothing in this build sends, submits, emails or writes to project Verify, proposals or RFQs. Auto send is untouched.
- Release checklist (for a later session): owner review of the golden report; staging dry run on one project; prod migration approval; flags flipped one at a time (facts, derive, consolidator tools, gates, pricing state test, review shadow, marked plans code); memory and release notes updated.

---

## 10. Risks

1. Scorer and truth noise: estimator truth has errors (3 known on 7154); classifier rules encode G3 conventions. Mitigation: adjudications with human review, bucket view, classifier gate on all 66 BOQs, MATCHER_VERSION with re-scoring.
2. Overfitting to 7154: every workstream's regexes come from it. Mitigation: 7154 declared a training case; core and holdout judge generalization; LOPO everywhere; holdout once.
3. Small n for go/no-go (8 plus 4 cases). Mitigation: the $0 LOPO pricing backtest on about 35 cases carries most pricing signal; results reported with per-case tables.
4. Route synthesis generality: stacked labels, home-run arrows, untagged plans, rotated enlarged plans, half-size sets. Mitigation: tag-convention classifier, registration with rotation, density fallback with warn, calibration on the training split, human-routed findings when unsure. Branch wire is about 60% of dollars, so this is the largest accuracy risk after the hours policy.
5. Hours policy (reviewer uplift): G3 hours carry judgment not in the drawings (18 of 38 reviews outside the clean band). Mitigation: calibrated on engine basis, range shown, provisional status, owner-tunable.
6. Text-layer coverage: about 22% of corpus pages are sparse, vector-outlined or scanned. Mitigation: `text_layer_quality`, census `unavailable` never 0, vision reading and recount there, OCR as a named follow-up.
7. Cost of tool loops on large contexts. Mitigation: small-context recount groups, deterministic pre-call stage, round caps from remaining budget, cache gate from round 3, run ceiling, ledger reservation.
8. Shared-file merge conflicts across nine workstreams. Mitigation: integrator owns hot files; hooks as single helper calls; no blind git operations.
9. Pricing data leakage to non-pricing roles through chat threads, payloads or tool results. Mitigation: sender-role gating, thread redaction, recursive key-scan tests, aggregates only in tool results.
10. Over-exclusion making a bid non-responsive, or under-scoping from breaker reuse. Mitigation: soft rules, needs_review flags (`breaker_reuse_assumed`, `raceway_conflict`, `fact_conflict`), one-click Include, false-drop metric <= 1%.
11. Live budget exhaustion before the holdout. Mitigation: dry runs, per-sweep budgets, stop conditions (section 0.4), contingency held back.
12. Scratchpad loss on reboot. Mitigation: setup step S1 first; this spec is self-contained.
