# AI Estimating autonomous build: final report

Status 2026-10-24 15:00 PT, written from `docs/AI_ESTIMATING_BUILD_LOG.md`, the ledger report, and the sweep results under `EVAL_ROOT/sweeps/`. DEV ONLY, committed locally on branch `build` in both repos, never pushed. No dollar figures from real projects appear here; USD amounts are API spend. The core sweep and the live gauntlet are done; the holdout has not run (stop condition 0.4 item 7, the owner decides).

## 1. Workstreams

File counts come from the no-dash manifest `tests/ai_estimating_new_files.txt` (305 paths after the 10-24 additions: 98 app modules, 34 scripts, 115 test modules and helpers, 33 fixtures, 7 migrations, 16 FE files, 2 docs). Existing files changed only through the integrator: `config.py`, `roles.py`, `features.py`, `deps.py`, `.env.example`, dev `.env`, `pyproject.toml`, `tests/conftest.py` (S3 and wave 1); `plans_reader_agents.py`, `plans_reader_runs.py`, `app/main.py`, `llm_health.py`, `llm_queue.py`, `file_access.py`, `plans_reader_golden.py` (waves 1 and 2); the wave 3 spine pass applied 88 collected hook edits (54 applied or adapted, 24 already done, 9 handed back, 1 superseded), changed 6 assertions in two existing test files, and merged 465 locale keys into all six locale files.

| WS | Status | New files | Main modules | Migration (dev) | Dev flags |
|---|---|---|---|---|---|
| S3 contracts | done | 10 | `estimating_contracts`, `plans_reader_tools`, `plans_reader_review_schemas`, `electrical_items`, `pricing_types` | none | none |
| WS0 eval | partial: harness, scorer v2, ledger, replay, integration and smoke done; core running, holdout pending | 39 | `plans_reader_score`, `eval_ledger`, `plans_reader_replay`, `plans_reader_acceptance`, `eval_run.py` | 0163_ai_eval_ledger (never ships) | `EVAL_ROOT`, `LLM_CASSETTE_DIR` set; ledger enforce, cassette mode, sweep id and claim types per process only |
| WS1 page facts | done; blind titles target missed | 39 | `app/services/pagefacts/` (24 modules) | 0164_plans_reader_page_facts | `PLANS_READER_PAGE_FACTS_ENABLED=true`, `PLANS_READER_FACTS_RELABEL=true` |
| WS2 tool loop | done; L1 and L2 run | 26 | `plans_reader_toolloop`, `plans_reader_tool_handlers`, `llm_parts`, `llm_anthropic` (no live run), `plans_reader_model_routes` | none | routes on code defaults; `anthropic_plans_refusal_fallback` true and inert |
| WS3 measure | done; B9 and markup targets missed | 16 | `plans_reader_circuits`, `plans_reader_routes`, `plans_reader_measure`, `plans_reader_measure_assist` | none | `PLANS_READER_DERIVE_ENABLED=true`, `PLANS_READER_MEASURE_ASSIST_ENABLED=true`; route_factor 1.00 and home run 150 FT unchanged pending the owner |
| WS4 consolidation and gates | done; live acceptance met | 51 (28 fixtures, 0165 included) | `plans_reader_merge`, `plans_reader_gates`, `plans_reader_consolidator`, `plans_reader_import_run.py` | 0165_plans_reader_autonomy_core (integrator) | `PLANS_READER_CONSOLIDATOR_MODE=tools`, `PLANS_READER_CODE_REGIONS=true`, `PLANS_READER_GATES_BLOCKING=true`; demo two-line style false by rule (11.1% of BOQs) |
| WS5 scope policy | done | 30 | `scope_policy`, `bid_conditions`, `scope_engine`, `scope_attribution` | 0166_ai_scope_policy | `AI_SCOPE_POLICY_ENFORCE=true`, `AI_SCOPE_ATTRIB_LLM=true` |
| WS6 pricing | partial: built and live on 7154; two program targets missed; chat probe not run | 36 | `cost_library`, `pricing_engine`, `pricing_run`, `vendor_quotes`, PricingTab | 0167_ai_pricing | `AI_PRICING_ENABLED`, `_AUTORUN`, `_CHAT_TOOLS_ENABLED`, `_AGENT_TOOLS_ENABLED`, `QUOTE_LINES_LLM_FALLBACK_ENABLED` all true; state test |
| WS7 review and recount | partial: built, recount live in the integration runs; gauntlet live pending; reviewer frozen | 21 | `plans_reader_bands`, `plans_reader_recount`, `plans_reader_reviewer`, `review_gauntlet.py` | 0168_plans_reader_review | `PLANS_READER_RECOUNT_ENABLED=true`, `PLANS_READER_REVIEW_MODE=apply` |
| WS8 evidence and UI | done apart from live chat checks; marks target missed; translation closed | 30 | `plans_reader_evidence`, `plans_reader_autonomy`, `plans_reader_annotated`, FE evidence and sign-off | 0169_plans_reader_evidence_signoff | `PLANS_READER_MARKED_PLANS_MODE=code`, `PLANS_READER_ANNOTATED_PLANS_ENABLED=true` |

All seven migrations were applied to dev (`bpidntbyvoooqvaispup`) through `apply_migration` with a PostgREST reload; none touched staging or prod. Dev-only data: 64 cost sources, 1,768 items, 3,665 observations; band version `hb1-20261021-bbd6157d` (19 rows) active.

## 2. Tests

Baseline before the build: 2 failed (known browser-agent pair), 8,436 passed, 1 skipped. Latest full run (2026-10-24, before the core relaunch): 2 baseline failures, 13,745 passed (with the other sessions' tests). Earlier: 12,071 passed before the core sweep, golden 74. The last run that listed xfails showed 6 strict xfails (the missed targets below). Totals include the concurrent assistant session's tests from 2026-10-22 on. Progression: 9,601 (wave 1), 10,888 (wave 3), 11,616 (re-run fixes), 12,071. ruff clean throughout. FE (last recorded, wave 3 fix pass): `npx eslint .` 0 errors with the 2 baseline warnings; build exit 0.

New test modules per workstream (manifest): S3 5, WS0 17, WS1 10, WS2 14, WS3 6, WS4 10, WS5 11, WS6 15, WS7 7, WS8 11, plus the spine suite. Counts the log gives: 25 spine tests; 52 new WS7 scripted tests in wave 3; 136 gate tests green after the orphan fix; the 25 golden-script tests unchanged after the identity move.

## 3. Acceptance on 7154 (run 087ac7a6)

Source: `attach_087ac7a6_1792717912/results.json`, re-run 2 from consolidating. Measured result: 26 pass, 1 fail (E13), 2 skip. The log reports this run as 28 pass, 1 fail, 2 skip, which sums to 31 on a 29-row table; the results file is used here (the earlier attach of the same run read 25 pass, 2 fail, with E11 failing before the census reader fix). Baseline is the section 3.5 weekend-bundle value.

| Check | Result | Measured | Since the weekend baseline |
|---|---|---|---|
| E1 | pass | priced artifact, 36 lines, priced share 1.0, total equals Verify sum | from no priced estimate |
| E2 | pass | #12 rel 0.172 acceptable; #10 rel 0.139 acceptable; EMT rel 0.029 good | from 0 / 0 / 0 FT |
| E3 | pass | 0 denial phrases; 6 FT lines all with a basis | from 4 hits |
| E4 | pass | prevailing wage, rate per Q5, 0 wage RFIs | from no wage decision |
| E5 | pass | 0 priced house exclusions, 0 extra allowances | from 8 of 10 extra allowances |
| E6 | pass | EMT 1.03 x truth; RGS, RMC, IMC 0; EMT clarification; 0 raceway RFIs | from no conduit |
| E7 | pass | FA and access not priced; excluded by others with Exhibit A cite | from Honeywell allowance carried |
| E8 | pass | 0 priced rows; every topic excluded with a citation | from telemetry line priced |
| E9 | pass | vendor quote, lowest eligible, reason recorded; L3 16 once, L5 50 FT, L1 21 | from no quotes |
| E10 | pass | L5 43.34 FT vs 43.46, good | from 7 EA |
| E11 | pass | demo lighting 48 of 49; ED3.01 troffers 17; no tile-subtotal rows; census met | from 34 |
| E12 | pass | one GFCI breaker line, 4 EA, 0 provisional | from 26 EA in 5 rows |
| E13 | fail | duplex, TR and GFCI lines cite the hospital-grade fact; the two occupancy-controlled lines have no cited fact and price below the floor | still fail (policy question) |
| E14 | pass | thermostats 5 exact; control cable 137.5 vs 141 FT, good | from both absent |
| E15 | pass | track demolition 50.01 FT measured vs 50.07, good | from 3 EA |
| E16 | pass | legend pages M0.00 and E0.00, 100 entries, 0 caveat rows (request part skip) | from no legend page |
| E17 | skip | the imported run's bundle has no source PDFs to measure marks; wave 3 evidence replay measured median 0.00 ft, 2 of 165 over 5 ft, 0 stacked | from median 3.8 to 6.6 ft |
| E18 | pass | 10 of 10 drawing pages labeled; facts done on 6 of 6 documents | from titles absent |
| E19 | pass | 0 unreadable hits; L4 control LOCAL; 0 questions | from false L4 RFI |
| E20 | pass | 0 maintain-vs-remove RFIs; 9 of 9 demo lines with decision and citation | from RFIs |
| E21 | pass | TR 3 exact; 0 modifier-split rows; receptacles 74 | from TR 0 |
| E22 | pass | 2 rows summing 34 (truth 35), no damper row | from 4 rows plus damper |
| E23 | pass | 144 connectors, 718 couplings (rel 0.029), 718 straps | from no accessories |
| E24 | pass | no mechanical set read as electrical; 22 of 170 spec pages sent | from 170 sent |
| E25 | pass | scorer v2, 0 unresolved collisions, 20 of 23 | was the v1 failure |
| GC1 | pass | WIRING, OTHER ITEMS, LIGHTING CONTROLS carried | from no pricing |
| GC2 | skip | no sent proposal text for the case (report only) | not run before either |
| GC3 | pass | not refused | not applicable before |
| GC4 | pass | exclusion sha matches, own project included | from field absent |

Against section 1.3 item 1 this run does NOT meet the 7154 smoke gate in full: count lines exact 20 of 23 (need 21); bid error +25.5% vs G3 sent (need within 10%); E13 fails and E17 skips although E17 has no skip allowance. Lengths (4 good, 2 acceptable, EMT good, track measured), dollar coverage 0.906 and dollar-weighted error 0.058 pass. The log's "integration gate met" refers to counts, coverage and acceptance only.

## 4. Golden results

7154 runs (truth 23 count lines, 6 length lines):

| Run | Kind | Exact | Within 1 | Lengths good/acc/miss/none | Coverage in-band (partial) | DW error | Bid vs sent | Acceptance P/F/S |
|---|---|---|---|---|---|---|---|---|
| weekend bundle | baseline | 17 of 22 raw (18 of 23 adj.) | | 0 of 6 | (0.319) | | none | 1 / 26 / 2 |
| 51ec302b | full run 1 | 11 | 12 | 2/2/0/2 | 0.718 (0.817) | 0.225 | no pricing in bundle | 12 / 14 / 3 (15 / 12 / 2 after re-export) |
| 2492ba7a | full run 2 | 19 | 21 | 3/2/1/0 | 0.894 (0.970) | 0.076 | refused | 19 / 8 / 2 |
| ad69e5a4 | re-run 1 | 18 | 21 | 4/2/0/0 | 0.849 (0.961) | 0.091 | refused | 22 / 5 / 2 |
| 087ac7a6 | re-run 2 | 20 | 23 | 4/2/0/0 | 0.906 (0.983) | 0.058 | +25.5%, hours +32% | 26 / 1 / 2 |
| b87f0ffd | full run 3 (noise stand-in) | 19 | 22 | 3/2/0/1 | 0.881 (0.956) | 0.083 | +22.5%, hours +28% | 24 / 3 / 2 |

Smoke:

| Case | Run | Exact | Lengths produced | Coverage | DW error | Bid vs sent | Acceptance P/F/SR/S |
|---|---|---|---|---|---|---|---|
| YMCA 7177 | 9bf717d4 | 4 of 13 | 0 of 17 | 0.239 | 0.761 | -6.1% | 11 / 12 / 0 / 6 |
| Five Star 7172, before fixes | 785ae29e | 5 of 40 | 3 of 16 (all miss) | 0.066 | 0.893 | refused | 8 / 14 / 1 / 6 |
| Five Star 7172, after generalization fixes | 3979444b (re-run) | 26 of 40 | 3 of 16 (1 acceptable) | 0.444 | 0.464 | refused | 10 / 13 / 1 / 5 |

YMCA is a service and site job the branch measure does not cover. Five Star exposed generalization defects (cross-category row-id collisions, panel titles, L-digit-only fixture tags, a flat home-run fallback), fixed at $0; a scripted replay of its stored rows reaches 30 of 40.

Refusals and why: run 2492ba7a, `unsourced_line` x2 (engine-inserted TR split and thermostat inclusion) plus `arithmetic`, scored false refusal by GC3, fixed with split-pair and keynote provenance; re-run ad69e5a4, `partial_basis`, `orphan_rows`, `arithmetic` from a device merge that folded two GFCI rows (a "non-GFCI" regex defect), fixed; Five Star 785ae29e, `arithmetic`, safe refusal; Five Star 3979444b, `unverifiable_citation` x4 and `arithmetic` x4 because the final gates indexed stored rows without the consolidator's aliases, fixed at $0 and not re-run.

$0 LOPO pricing backtest (holdout-free, 33 cases): library strong-or-good share of basis hours 44.0% (target 70%); hours within 10% on 30.3% (65%), median abs error 16.6% (8%); false-confident 27.6% of 557 lines (5%); 1.3 item 4 oracle median abs error 15.7% (7%), 30.3% within 10% (70%), 0 refusals. Golden Verify band passes (clean n 11, median 7.4%, 72.7% within 10%). Verify replay 43 of 45 within one dollar. PG-A2 1.5% below the corrected total (3% gate met); PG-A3 16.5% above the corrected total (reported).

Hours policy re-cut on 2026-10-24 ($0, holdout never read): the single 1.20 uplift conflated two regimes, so the engine now keys the uplift on the estimate's own coverage: 1.10 where the engine covers the estimate (reviewer round-up, 9 of 10 clean pairs) and 1.32 where it is under-covered (4 clean pairs). Leave-one-out on the 23 training pairs: median abs error 23.1% to 15.4%, within 10% 62% to 69%. Effects: 7154 PG-A3 +20.7% to +12.8% vs sent (+8.8% vs corrected); run 087ac7a6 re-priced +25.5% to +20.2% (reviewer hours 825 to 775); golden Verify band median 7.2% to 5.2% with 72.7% within 10%; pool oracle within 10% 27.3% to 30.3% (median unchanged). The remaining 7154 gap is this estimator's p25 unit hours and small round-up; no pool-supported calibration closes it. The core sweep (attempt 2) ran on this calibrated code.

CORE SWEEP (attempt 2, sweep 01813bde, 2026-10-24, 54.85 USD): four of five cases ran; 7158 was stopped by the blind guard before upload (its quote folder holds G3's own RFQ PDFs and markup; fixed in the manifest notes, re-run on its own sweep, see below). Attempt 1 (10-22 to 10-24) ended on an unclaimed bid_split job and a network drop and spent 4.78 USD on this sweep before the relaunch.

| Case | Tier | Run | Bid error vs sent | Hours error | Count exact | Length bands | Dollar coverage (partial) | Refusal |
|---|---|---|---|---|---|---|---|---|
| 7181 lighting controls retrofit | core | 10f9599c | -12.7% (miss band) | 40 h = G3 (good) | 0 of 5 (truth 7) | 1 of 2 produced, miss | 0.00 (0.26) | none |
| 7196 streetlight and signage | core | 69ca79f8 | refused (counts as miss) | none | 0 of 6 | none | 0.00 (0.00) | safe refusal `new_devices_no_branch`; GC3 false_refusal (no reference defect matches: civil scope) |
| 7167 signs and wall packs | core | 843f11dc | +383% | 625 h vs G3 none, estimator x9.3 | 1 of 4 (truth 7) | 2 of 3 produced, miss | 0.04 (0.51) | none |
| 7179 lighting retrofit, nLight | core | 478fa524 | refused (counts as miss) | none | 20 of 51 (within 1: 23; truth 56) | 0 of 5 produced | 0.42 (0.69) | safe refusal `new_devices_no_branch; partial_basis; orphan_rows` |
| 7158 veterinary hospital TI (132 truth lines) | core | cf2ef066 (sweep cdf595c7, 18.11 USD) | refused (counts as miss) | none | 13 of 99 (within 1: 16) | 5 of 33 produced (2 good, 3 miss) | 0.17 (0.26) | safe refusal `unit_class; partial_basis; uom_conflict_quote`; done_with_errors: one recount group failed (page image too large for the provider) |

Core verdict against 1.3 item 2 (median absolute bid error <= 10%, 60% within 10%, at most one refusal, none worse than 25%): missed on every clause. Three refusals of five, one case at +383%, the best case at -12.7%. Stop condition 0.4 item 7 (median above 20% after core) holds: the holdout waits for the owner.

Causes, from the score lines (no model call): 7167 carried two existing panels and 58 breakers as new work by a binding scope-policy carry and the measure ran branch for every breaker (#10 21,285 FT, EMT 7,260 FT against truth 763 FT), the same schedule family as Five Star; 7181 read the nine "disconnect existing lighting controls" rows as control modifications, split the five receptacles by mounting height, missed the breakers and assumed #10 where the estimator used #12, while hours still landed on G3's 40; 7196 is site and streetlight work with no branch to measure (like YMCA's feeders), so the pricing gate refused; 7179 matched most of the fixture schedule exactly but folded the E-suffixed emergency variants into their base tags, rendered the nLight control products (switches, sensors, power packs) as generic occupancy-sensor rows the matcher could not assign, and produced no branch, so pricing refused; 7158 (the one full tenant-improvement set) read the lighting schedule almost exactly (L3 48, L4 102, L11 10, X 26 and most other tags) and found all eight panels and the switchboard (the matcher filed them as extras because the AI names them by designation while the estimator names them by rating), but one recount group failed (page 3 at 150 ppi rejected by the provider as too large, so that page kept its first-pass count), receptacles came out at a fraction of the estimator's (duplex 0 of 282, quad 20 of 138), data outlets, switches and sensors were almost all missing, no feeders, and pricing refused on a unit class and two quote unit conflicts. The recount pass and the reviewer ran on every case (two or three recount groups each) and did not change these outcomes.

HOLDOUT SWEEP: pending the owner's decision (7188, 7173, 7178, 7148; once, on the committed tree with the assistant build's files on a recorded allow list).

GAUNTLET (WS7, sweep 837a9a00, 2026-10-24 14:30 to 14:43 PT, 7.04 USD of 15): the frozen reviewer (gpt-6-astra medium, tools on, 4 USD cap per review) ran in-process on the four stored 7154 bundles (51ec302b, 2492ba7a, ad69e5a4, 087ac7a6), each as a clean control plus the two seeded E-class variants the pack builder supports (E2 branch conductors removed, E4 wage condition broken). Attempt 1 on 10-22 (sweep 3939c072) ended before any call on a None client and spent nothing. Spec WS7 acceptance: catches the seeded defects in at least 70% of variants, at most 1 false critical per clean control, review cost at most 4 USD per run.

| Pack (bundle) | Variant | Seeded error | Caught | Findings | False criticals | Tool calls | Seconds |
|---|---|---|---|---|---|---|---|
| 7154 087ac7a6 | clean | none | control | 7 | 1 | 10 | 44 |
| 7154 087ac7a6 | E2 | branch conductors removed | yes | 8 | 0 | 10 | 52 |
| 7154 087ac7a6 | E4 | wage condition broken | yes | 6 | 0 | 10 | 41 |
| 7154 2492ba7a | clean | none | control | 8 | 0 | 9 | 76 |
| 7154 2492ba7a | E2 | branch conductors removed | yes | 7 | 0 | 10 | 55 |
| 7154 2492ba7a | E4 | wage condition broken | yes | 7 | 0 | 9 | 66 |
| 7154 51ec302b | clean | none | control | 8 | 1 | 10 | 56 |
| 7154 51ec302b | E2 | branch conductors removed | yes | 8 | 0 | 10 | 60 |
| 7154 51ec302b | E4 | wage condition broken | yes | 7 | 0 | 10 | 63 |
| 7154 ad69e5a4 | clean | none | control | 7 | 0 | 10 | 37 |
| 7154 ad69e5a4 | E2 | branch conductors removed | yes | 8 | 0 | 9 | 73 |
| 7154 ad69e5a4 | E4 | wage condition broken | yes | 6 | 0 | 8 | 63 |

Result: 8 of 8 seeded variants caught (100%, target 70%); 2 false criticals over 4 clean controls (0.5 per control, no control above 1); 0.59 USD per review (12 reviews, 7.04 USD); 0 errors. The WS7 reviewer acceptance is MET on these packs. Caveats: all four bundles are 7154 runs (the Five Star and YMCA bundles were not included; `--bundle` flags would add them), only the E2 and E4 seeds exist, and three bundles ran on the text-layer fallback because their stored documents carry no page facts (the 2492ba7a bundle had facts). Results: `EVAL_ROOT/reports/gauntlet/20261024_143055/gauntlet_results.json`.

## 5. Spend

Ceiling 400.00 USD, spent 194.46, remaining 205.54 (ledger report, 2026-10-24 15:00 PT). Checkpoints: probes 40.04 (limit 108); cumulative after integration, noise stand-in and smoke 114.46 (limit 216); after core and the gauntlet 194.46. The holdout (73 USD) would leave about 132 USD.

| Workstream | Allocated | Spent | Remaining |
|---|---|---|---|
| WS2 | 29.00 | 10.58 | 18.42 |
| WS3 (14 from contingency) | 20.00 | 14.12 | 5.88 |
| WS4 | 26.00 | 11.59 | 14.41 |
| WS5 | 6.00 | 0.11 | 5.89 |
| WS6 | 8.00 | 3.64 | 4.36 |
| WS7 | 28.00 | 7.04 | 20.96 |
| WS8 | 5.00 | 0.00 | 5.00 |
| Contingency | 0.00 | 0.00 | 0.00 |
| Shared | 278.00 | 147.39 | 130.61 |

| Workstream | Sweeps (id spent) |
|---|---|
| WS2 | L1 8bc7a61e 0.03; L2 a9e8a89c 5.55; L2 repeat 2d9da491 5.00 |
| WS3 | assist bfae814e 0.22; B12 72cc2b7b 6.11, f694fef6 7.79 |
| WS4 | iterations 777f88cb 2.43, 37fa4918 1.24, 7aebab69 1.11, 208863fc 2.92; main A/B a064650c 1.05, db2ad49b 1.68, 91e7d7ad (YMCA) 1.15 |
| WS5, WS6 | attribution b1721e86 0.11; quote fallback 1f7bed54 3.64 |
| WS7 | gauntlet 837a9a00 7.04 |
| shared | integration 1 8888aad1 12.26 and chain e5695cad 0.86; integration 2 ace9a2cd 14.29; re-runs 53be8304 2.61, 7601c069 3.16; smoke 78f334dc 22.16, 1658b6ec 15.64; Five Star re-run e744c68c 3.46; core 01813bde 54.85 (attempt 1 4.78 included); core 7158 re-run cdf595c7 18.11 |
| zero spend | 6 sweeps: refused or aborted starts and gauntlet attempt 1 3939c072 |

Ledger sweep totals count every call stamped to the sweep and can exceed the per-run costs in the log. Sweep 208863fc includes 24 calls (2.78 USD) that the owner's port 5051 dev server ran unenforced, stamped to the sweep by SQL on dev.

## 6. Model choices

All live calls ran on OpenAI (owner, Q3). Spec routes naming `claude-opus-5-5` were replaced; the Anthropic transport exists as code with fake tests only.

| Step | Route | Evidence |
|---|---|---|
| sub, escalation | gpt-6-astra medium | fixed by spec |
| recount | gpt-6-astra medium; per-group cap raised 0.80 to 1.50 USD | L1: gpt-6-astra and gpt-5.4-mini pass all four checks. L2 round 1: 7 of 12 pairs truncated; clean 5 split 3 both, 1 each. Repeat at 1.50: A clean 6 of 7, B truncated 5, errored 2. A exact 7 of 12 plus 2 within 10%; B 5 of 12 |
| main consolidator | gpt-6-astra medium | attempt 1 invalid (harness offered 18 of 31 tools). Attempt 2 on 7154: A passed first finish, 36 lines, 1.38 USD; gpt-5.4 truncated with 0 lines. YMCA: both passed; the rule picks B there, but B fails the 300-row case |
| review | gpt-6-astra (code route, same model as the builder: the deviation's fallback); no review A/B; gauntlet 8 of 8 caught, 0.5 false criticals per control, 0.59 USD per review | live chains: verdicts advisory or needs_human, 10 rounds, 0.93 to 1.33 USD per review where the log gives it alone |
| revise | gpt-6-astra | fixed |
| scope attribution | gpt-6-astra low | probe b1721e86: 21 of 21 accepted |
| quote fallback | gpt-6-astra | probe 1f7bed54: 29 of 32 parsed, 3 referenced quotes exact |
| derive assist | gpt-6-astra | probe bfae814e: 4 asked, 0 picks; changes nothing on the corpus |

## 7. Deviations, defaults, decisions, open items

Deviations: (1) all-OpenAI routing; (2) WS7 bands measure 2.33 / 3.46 / 6.05 hours per device (the prototype read only the first labor block); (3) WS6 targets missed (below); (4) recount cap 1.50 USD instead of 0.80; (5) YMCA's 5-page addendum belongs to 7171 and is excluded, so 7177 has 7 big sheets; (6) dollar coverage printed in both definitions, the in-band one gates; (7) hard seed rules with an `unless` became soft (5.8 forbids it); (8) PF7 maps 33 numbered spec sections, not 34; (9) E3.01 reads 45 tags on 2 circuits, not 44 on 3; (10) S2 seeding moved to wave 4 (spend hazard); (11) the third full 7154 run stands in for the noise repeat because the smoke tier includes 7154; (12) core opens before the gauntlet's second attempt, with the reviewer frozen; (13) recount re-merge is not wired for finished runs; (14) the hour uplift is two-regime by coverage (1.10 / 1.32) instead of one constant; (15) the holdout's clean-tree rule takes an allow list of the assistant build's uncommitted paths, recorded in the sweep config, because that build shares the tree; (16) pricing-stage uploads drop re-attached drawings and inline email images, and 7158 carries two documented blind overrides for vendor proposals whose names say PROPOSAL.

Defaults used (spec 2.2): Q2 tiers frozen, Q4 sent truth, Q8 PG-A2 gate, Q9 lowest eligible quote, Q11 seed grades (eval runs take the reference seeds), Q14 thermostat inclusion beats the mechanical fact, Q16 code owns branch rows, Q17 drops 0, Q18 relabel in dev, Q20 override roles, Q21 apply in dev, Q25 proposal lines as suggestions, Q26 sign-off by VERIFY_ROLES, Q27 7154 fixtures committed, Q29 both plan outputs; the E3.01 tag count is a measured default for the owner to restate.

Owner decisions received: at start, Q1 (Executive, Estimating Engineer both focuses, IT Admin review adjudications), Q3 (split approved, all live spend on OpenAI), Q6 (markup editable); on 2026-10-22, the 5051 server may be stopped, no translation needed, B12 approved with 14 USD from contingency, the recount request stays recorded on finished runs, commits allowed before the holdout (other work first, then this build).

Open items:
- E13: the two occupancy-controlled receptacle lines are outside the seed keys (policy question).
- Demolition lighting 48 of 49: C26 and C28 may count the same entry lights twice (reading question).
- YMCA: feeder and site measurement and gear vocabulary are outside this build.
- Five Star: switched and GFCI receptacles are legend knowledge the symbol rows do not carry; junction boxes, ground rod and weld have no rows.
- Translation closed at 0 USD; English placeholders stay.
- B12: draft at `EVAL_ROOT/manifest/scales_draft.json`, 330 labeled, 34 unresolved, 332 pages unsent; not in the live product.
- Checks tab recount button stays uninstalled until a reopen path is designed.
- Missed $0 targets: WS6 LOPO fidelity and 1.3 item 4 (numbers in section 4; open owner questions: keep 1.3 item 4 as a gate or report it, and allow a per-project productivity factor); WS3 B9 conduit within 15% on 31.2% (60%), within 30% on 50.0% (85%), wire within 20% on 43.8% (60%), route_factor 0.90 and home run 60.1 FT await sign-off; WS1 titles 18.3% (90%); WS8 marks precision 0.762, recall 0.661 (0.85); WS3 markup truth 6 of 8 pages (80%).
- 7154 bid error +20.2% (two-regime re-price of 087ac7a6; +25.5% as run) and +22.5% on the noise stand-in, hours +24% to +28%, against G3.
- Core attempt 2 (section 4): every 1.3 item 2 clause missed; defects are generalization (scope policy binding carries of existing panels, per-breaker branch fallback, control-product vocabulary, E-suffix fixture variants, no site or feeder measure). Tuning is frozen; the owner decides where the remaining budget goes.
- Done since the draft: the two commits in both repos (BE 89b807c and cd1cc81, FE 632d5a7 and f436069, local only), the `PLANS_READER.md` section 8.7 entry, the 7158 manifest fix.
- Still to do: the owner's holdout decision (73 USD, once, on the committed tree with `--allow-dirty-paths EVAL_ROOT/handoff/holdout_allow_dirty.txt`), or the same budget on the core defects above; after that the memory note's final line.
- Known small defects from today: the recount pass sends a page's original render to the provider and one 7158 page was rejected as too large (tile or down-sample above the limit); the run status text over-states a failed recount as missing reading pages; the sweep wrapper must never close a sweep on a runner crash (now a separate step).

## 8. Memory

Feature note: "BDR AI Estimating Autonomous Build" (`bdr-ai-estimating-autonomous-build.md`), updated 2026-10-24 with the commits, the core verdict, the stop condition and the holdout allow list. DEV ONLY until released.
