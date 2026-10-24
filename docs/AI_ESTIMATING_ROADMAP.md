# AI Estimating Roadmap (plans reader, next 26 weeks)

Superseded for the autonomous build by docs/AI_ESTIMATING_BUILD_SPEC.md (2026-10-21). Kept for background and the capability survey.

Status: PLAN, 2026-10-21. Nothing in this doc is built. No migration, no code, no flag. It extends `docs/PLANS_READER.md` (the design, build contract and build log of the shipped feature) and does not replace any of it. Audience: the G3 owner, who picks what gets built, and the developer who builds it.

Sources: a read-only survey of Bidwright (github braedonsaunders/bidwright, TypeScript, AGPL-3.0), nine fit analyses that compared each Bidwright capability with our code and with the 2026-10-01 prod backup on disk, three competing roadmap drafts (accuracy-first, value-first, risk-first) and a critic pass that merged them. Every number marked "measured" comes from our own files or our own runs; every number marked "estimate" still has to be checked on the golden set.

## 1. Purpose and owner decisions

AI Estimating reads a bid set and produces the BOQ, the estimate workbook (pricing columns blank), trench and saw cut markups, marked plans and a deep brief. The owner likes this setup and its goal. This roadmap makes it more accurate, more auditable and more useful to estimators, and adds suggested pricing as a separate, internal layer.

### 1.1 Binding owner decisions

1. **Survey everything, owner picks.** Every Bidwright capability is ranked adopt, adapt, skip or later for an electrical subcontractor that bids from GC PDF sets in Las Vegas. Section 5 lists all 99. The owner chooses what gets built; this doc recommends an order.
2. **Quantities plus SUGGESTED pricing.** Labor units and material costs are suggested from G3's own past bids, each with provenance (which projects, how many, how recent) and a confidence label. Human estimators stay in charge of price. There is no full AI pricing: no model writes a price into a deliverable, and no suggestion is ever prefilled into a G3 pricing step.
3. **Any model provider per step.** We are not locked to GPT-6 Astra. Each step gets a cost estimate. In practice most of this roadmap is deterministic code at $0 per run, and a second provider only enters when a golden A/B and the owner's data decision allow it.
4. **Learn, do not copy.** We take approaches from Bidwright. We do not copy its code and we do not translate it line by line. Every design below is native to our Python, Supabase and Next.js stack and to the plans_reader architecture: queue jobs (`llm_jobs`, `PLANS_JOB_TYPES`), group kinds (`GROUP_KINDS`, `plans_reader_agents.py:95`), strict JSON schemas (`plans_reader_schemas.py`), the spend cap (`ai_spend.py`), and the deterministic render pipeline (`plans_reader_render.py`).

### 1.2 License and data hygiene

- Bidwright is AGPL-3.0. Copying or translating its code into BDR could pull BDR under AGPL obligations. This doc contains no Bidwright code, only descriptions of approaches in our own words and file paths for reference (appendix A).
- Bidwright's Python vision tools depend on PyMuPDF, which is also AGPL. We do not add PyMuPDF. Everything below uses libraries we already ship (pypdfium2, pypdf, openpyxl, Pillow) or permissive ones (rapidfuzz, and optionally opencv-python-headless or ezdxf if a later spike justifies them).
- Bidwright files were treated as untrusted data: read, never executed, installed or built.

### 1.3 Hard rules that apply to every item

- The external estimator role never reaches any pricing benchmark, suggestion, audit or dollar-weighted metric. AI Estimating already refuses that role; pricing adds a second, narrower gate (section 7.8).
- Every migration goes to the dev BDR database first. BDR_Prod only with explicit owner approval, one migration at a time.
- No em dashes in code comments, UI copy, files or docs.
- Pacific time for every internal date. UI strings in all six locales.

## 2. Where AI Estimating is today

Shipped 2026-10-18 (BE f09bc45, FE faff26a; migrations 0152 and 0153 on all environments). Prod runs at `AI_ESTIMATING_STATE=test`, which admits Executive and IT Admin only (`core/features.py:62`).

### 2.1 Strengths (keep all of them)

- Robust orchestration: compare-and-set state machine (`plans_reader_runs.py:2244 advance`), self-repair after 600 s, crash-safe `start()`, `resume` after a cap hit, one `llm_jobs` job per group with lease renewal and token pacing.
- Exact token planning: patch math mirrors the PDFium render, so planned 5-sheet groups landed within 0.5% of actual tokens. Groups stay under 240k input, below the 272k 2x billing line.
- Adaptive resolution with targeted 260 ppi escalation of the regions the model flags.
- Deep G3 domain prompting and house-exact deliverables: the BOQ and estimate workbooks match the sent files, fittings and wastage come from code (`plans_reader_render.py:133 boq_quantity`, `:176 conduit_fittings`), markups are real PDF annotations.
- Strong safety: document text is data, not instructions; modify needs the user's own words plus a writer role; no price field anywhere (`_NO_PRICING`, `plans_reader_agents.py:147`).
- Full accounting: one `plans_reader_calls` row per call, a $75 daily cap checked before every call, about 470 mocked tests.

### 2.2 Measured baseline

| Measure | Value |
|---|---|
| Five Star Resolution (25 sheets, no specs) | $25.99; 18 of 56 estimator lines matched fuzzy (SequenceMatcher 0.72), 15 of 56 exact; lighting 13 of 13 |
| YMCA Torrey Pines (7 sheets, scanned addendum, exhibit) | $6.65; 9 of 30 lines matched; 3.6% mean error on matched rows only |
| Cost basis | gpt-6-astra $10 in / $1 cached / $50 out per M; whole request 2x input, 1.5x output over 272k |
| Typical run | about $26 per 25-sheet 42x30 set; daily cap $75 (it refused the third golden run, Edikted) |
| Scanned pages | about 8,400 tokens each against the 1,100 the estimate assumes |

Precision has never been quoted. Only two live golden runs exist.

### 2.3 The key gaps

1. **Reading:** every count and length is one visual pass at 133 to 188 ppi. The PDF text layer is never read, although about 77% of our electrical pages carry one (measured, 229 pages from 60 PDFs; a 107-page sample said 82%).
2. **Legends:** with two or more large legend pages, all ledger images drop to `detail: high`, about 44 ppi effective. Labeled sets get no legend pages at all.
3. **Lengths:** no scale calibration and no measurement. The markup line and its printed footage are two independent model guesses.
4. **Merging:** the LLM roll-up re-emits rows at $50 per M output; the fallback `_merge_outputs` (`plans_reader_runs.py:1753`) concatenates with no dedupe; escalation patches are applied only by prompt ("patch wins").
5. **Provenance:** `BOQ_ITEM` has only free-text `sheet_refs`. Positions travel as `at (x,y)` text no code parses, so marked plans show one dot per legend row, not one per device.
6. **No accuracy regression and no learning loop:** all tests mock the model, and estimator corrections are never captured.
7. **No pricing at all,** while 76 estimator workbooks (68 projects) hold about 3,422 priced lines with unit labor hours and unit material cost.
8. **Spend leaks:** splitter calls are outside the cap (no Anthropic price rows, and `ai_spend` sums only `plans_reader_calls`, `ai_spend.py:73`).
9. **Silent losses:** a sub group cut off at the output cap fails for good, and the run error does not name the lost sheets.

## 3. What Bidwright does well

Bidwright is an open-source estimating platform that runs a coding-agent CLI (Claude Code, Codex and others) against about 186 MCP tools inside a per-project workspace. It covers more trades than ours and much more surface (3D, CAD, rate books). For a PDF-bidding electrical sub, its value is in a handful of contracts and patterns, not in its electrical math, which is close to nonexistent.

| Area | What it does | Maturity as verified |
|---|---|---|
| Evidence and provenance | Every image the model sees is a server-recorded view (page, box, dpi, hash, source checksum). Claims cite view ids, and the server fills provenance itself. Takeoff marks link to estimate lines with a multiplier; the server recomputes line quantities | Solid. The strongest part of the system |
| Integrity gates | Row writes are refused only when a citation is not real or the arithmetic does not reproduce; finalize compares structured summary numbers with server totals (2% tolerance) | Solid. Note: it checks structured fields, not prose |
| Review loop | A fresh-context read-only reviewer, then exactly one revision turn with addressed/declined dispositions; advisory, never blocks | Solid mechanism. Spec refs are free text, coverage score and money impacts are LLM strings, one-click apply has no preview and swallows errors |
| Eval harness | Blind copies, frozen human-reviewed reference, exact counts, 5% lengths, link integrity, model matrix pinned to a deployment | Solid. The outcome band score is only a smoke signal by its own README |
| Drawing reading | Page reader with upright image, positioned text, layout regions, tile grid; legend-to-template library; OpenCV matching; scale regex plus dimension lines; vector snap and trace | Mixed. Page reader solid. Template matching has no rotation. Vector work aborts above 2,000 paths, which 64 to 69% of our pages exceed. Legend extraction needs Azure DI and drops graphic-only glyphs, which is most of an electrical legend |
| Knowledge | Personas as trade playbooks, authored pages, books, typed datasets, keyword-favored retrieval with size and number anchors, document coverage telemetry | Solid for retrieval and coverage. The seeded electrical persona is generic industrial content. The vector lane was off by default |
| Pricing | Labor units with source refs, sourced/adjusted/assumed grading, typed condition factors, cost evidence vs derived cost, review-gated vendor quote ingest | Solid models. The weighted cost matcher is dead code; the match-quality label is lexical only; no C or M units (per 100 ft, per 1000 ft), which electrical wire and conduit need |
| Calibration | AI baseline snapshot vs human final, human-written lessons approved before reuse | Partial. Snapshots are whole-estimate aggregates, so it never learns "AI undercounts duplex receptacles" |
| Runtime | CLI harness with watchdog, resume and no-final-answer recovery | Solid supervision, but no strict schemas, no dollar budget per run, and a shell-capable agent next to service keys |
| 3D/BIM/CAD | Forked CAD and BIM editors (about 470k lines), IFC/DWG ingest, topology rollups | Solid but irrelevant: zero DWG, DXF, IFC, RVT or NWD files in 3,900+ backed-up G3 project files |
| Marketing vs code | Several README features are library code nothing calls: contextual chunk enrichment, reranker, sensitivity drivers, the 13-stage gated run plan, the weighted cost matcher | Treat as ideas only |

The posture worth learning: arithmetic and integrity in code, judgment in the model, provenance filled by the server, and review that advises instead of blocking. Our pipeline already has the right skeleton for all four.

## 4. Capability-by-capability assessment

How to read a block. **Verdict** is adopt (take the approach as is, in our design), adapt (take the idea, change the mechanism), later (worth it, but not now, with a trigger) or skip. **Effort**: S up to 3 days, M up to 2 weeks, L up to 4 weeks, XL more. **Phase** refers to section 6 (P1 to P6, Track B). Costs are per 25-sheet 42x30 run unless stated. "Critic correction" marks where the merged roadmap overruled a fit analysis.

Later and skip items appear in a short table at the end of each cluster; section 5 has the full ranking.

### 4.1 Measurement, evaluation and the learning loop

Bidwright's eval harness is the most transferable part of its review layer. Ours must be built first, because every later claim needs a baseline.

**Corrections to the research.** "Five Star 18/56" is fuzzy row recall; exact is 15/56 (27%) and precision was never quoted. Blindness is already structural: `REFUSED_PROJECT_CATEGORIES` (`plans_reader_runs.py:142`) refuses answer-file categories; the remaining leak is an answer file filed under the wrong category. Bidwright's lessons learn whole-estimate deltas, not per-device bias.

#### 4.1.1 Golden harness: manifest, blind guard, holdout, runner
- **Verdict:** adapt. **Phase:** P1. **Effort:** S.
- **Bidwright approach:** blind copies of live projects with answer files removed, and a frozen reference that must be marked reviewed before it can score.
- **Our design:** a versioned JSON manifest beside the backup (not a table) with tiers smoke 4, core 12, holdout 15 (fixed now, never touched for tuning) and full 66. Each case records input and answer-file sha256s. A blind guard refuses a run when any input hash matches an answer hash or a filename matches `BOQ|COUNTS|ESTIMATE|MARKED|TAKEOFF|MARKUP`. The runner loops `scripts/plans_reader_live_run.py` under `caffeinate`, with `--run-id` attach and a client-side budget.
- **Different:** our blindness is enforced by category already; the guard closes the misfiled-file leak. No eval table and no `purpose` column in v1.
- **Value:** every later phase is judged against this. **Model and cost:** smoke about $50, core about $180 to $250.
- **Risks:** estimator style is not truth (section 8.4). **Success metric:** zero blind-guard violations; core-12 baseline published.
- **Critic correction:** the fit analysis proposed `plans_reader_eval_cases` and sweep tables plus a `purpose` split in `check_cap`. Dropped for now: evals run on dev only, the dev cap is raised with the existing `set_cap`, and the runner enforces the owner-approved budget.

#### 4.1.2 Per-kind, aggregate and dollar-weighted scoring
- **Verdict:** adopt. **Phase:** P1. **Effort:** M.
- **Bidwright approach:** exact counts, 5% lengths, unit correctness, link integrity, duplicates, with geometric kinds (count, length, area).
- **Our design:** move `normalize_description`, `key_tokens` and `match_rows` (`scripts/plans_reader_golden.py:170, 275, 537`) into `app/services/plans_reader_score.py` with a `MATCHER_VERSION`. Kind = BOQ subgroup plus the key-token class, with overrides for feeder vs branch conductor (by AWG) and fixtures (by tag). Metrics per kind: exact rate, within 1, banded length error (10% good, 25% acceptable), gear rating identity, signed bias, and a granularity-robust aggregate per key-token bucket (so "receptacles split by room" vs "receptacles total" stops reading as a miss). A per-line Detailed Estimate parser normalizes wastage and gives a dollar-weighted error: |AI qty minus est qty| x (est unit hours x $108 + est unit material).
- **Different:** electrical kinds, not geometric ones; lengths banded, not 5%.
- **Value:** separates counting error from row style. **Cost:** $0.
- **Risks:** dollar weights are estimator pricing. They stay in local eval output only, never in the app or exports. Track B reuses the parser. **Success metric:** per-kind baseline with a documented noise band.

#### 4.1.3 Stage-wise attribution and miss classification
- **Verdict:** adopt. **Phase:** P1. **Effort:** S.
- **Bidwright approach:** a detected, saved, linked funnel for one page analysis.
- **Our design:** for every estimator line the AI BOQ missed, search the stored category outputs. Four labels: `never_seen` (no sub row), `lost_in_consolidation` (a sub row exists, main dropped it), `derived_line_not_produced` (fittings, makeup, conductor rows that needed math), `granularity_mismatch`. A second pass classifies Five Star and YMCA misses into reading, convention and measurement causes. Per-kind cost split from existing `plans_reader_calls` rows.
- **Value:** decides whether to invest in reading or in consolidation, and whether source_refs (4.5.3) are justified. **Cost:** $0. **Success metric:** 100% of Five Star and YMCA misses classified by cause and stage.

#### 4.1.4 Noise floor, estimator noise ceiling, splitter eval, replay test
- **Verdict:** adopt (critic additions). **Phase:** P1. **Effort:** S.
- **Our design:** run 2 to 3 smoke cases 2 to 3 times each for run-to-run variance. Measure the estimator's own round-to-round variance on projects with multiple sealed BOQs (the ceiling we can never beat). Measure splitter page-role and category accuracy with the existing `bid_split_training_examples`. Add a $0 CI replay test that re-scores stored v1 specs, and a $0 backtest for every new pure-function check.
- **Different:** Bidwright runs repeated "relentless loops"; at $26 a run we measure noise once.
- **Risks:** small n; no per-kind 2-sigma rules, aggregate-level rules only. **Cost:** about $100 to $150 inside the P1 eval budget.

#### 4.1.5 Correction capture (AI baseline vs estimator sealed round)
- **Verdict:** adopt. **Phase:** Track B1. **Effort:** M.
- **Bidwright approach:** freeze an aggregate snapshot at finalize, diff later human edits.
- **Our design:** our "human final" is the estimator's sealed BOQ, which arrives days later through the estimator portal. Hook the existing `auto_send_hooks.on_estimator_round` (`auto_send_hooks.py:29`), which already branches on `counts["boq"]`. A background task finds done runs linked to the project, scores the newest sealed BOQ with `plans_reader_score`, and writes `plans_reader_feedback` (run, project, AI v1 deliverable, estimator file and sha256, matcher version, per-kind summary, row pairs, input-set match, `input_changed` flag when the round's drawings differ from the run's inputs, adjudication status `ai_false_positive | estimator_miss | ambiguous`). Add `POST /ai-estimating/runs/{id}/link-project` so upload runs can pair. The v1 deliverable is already append-only, so no snapshot table is needed.
- **Value:** every live bid becomes a free eval case. **Cost:** $0.
- **Risks:** prod has little live signal while the state is test (section 10, risk 12). **Success metric:** feedback rows land within 5 minutes of a sealed round.

#### 4.1.6 Bias report and owner-approved prompt rules
- **Verdict:** adapt. **Phase:** P6. **Effort:** S.
- **Bidwright approach:** human-written lessons, approved, retrieved by tag overlap; no cross-project aggregation.
- **Our design:** a per-kind signed bias report from `plans_reader_feedback` once there are at least 10 pairs ("receptacles: AI -14% median over 9 jobs"). The developer drafts prompt rules from it, the owner approves, and rules are batched into one core sweep per quarter.
- **Critic correction:** the fit analysis proposed a `plans_reader_lessons` table, LLM-drafted lessons and per-lesson A/B gates. Deferred (later): there is no paired data yet, and quarterly batching is cheaper than per-lesson sweeps.
- **Success metric:** a quarterly sweep that improves the target kinds with no kind worse than noise.

#### 4.1.7 Later in this cluster

| Capability | Why later | Trigger |
|---|---|---|
| Accuracy tab on AI Monitor, sweep tables | Runner reports suffice | 5 or more sweeps exist |
| Full model and config matrix | Waits for a second transport | Anthropic transport approved |
| Calibration lessons engine (LLM drafting, A/B) | No paired data | 10+ feedback pairs per kind |
| Spatial placement scoring, visual probes | Estimator marked plans are flattened JPEG pages | Probes can be built at $0 from stored outputs first |

### 4.2 Drawing reading foundations

**Measured facts that drive this cluster:** 77% of sampled electrical pages have a real text layer, 14% are near-empty (5% image-only scans, the rest SHX text drawn as strokes), 69% have more than 2,000 path objects (median 7,510). 27 of 60 sampled PDFs carry AutoCAD SHX Text annotations whose `/Contents` hold real strings. A panel schedule came out of the pdfium text page as clean row strings.

**Corrections to the research.** BDR already extracts raw pdfium text in the RFP sandbox (`app/sandbox/render.py:150`), and `pypdfium2` is already a dependency. Bidwright's component census is 49 lines of generic table counting and counts distinct tags, not occurrences. Its legend extractor drops graphic-only glyphs and requires Azure DI. Its template matcher has no rotation search.

#### 4.2.1 Positioned text layer, SHX harvest, page classes, sanitization
- **Verdict:** adopt. **Phase:** P2. **Effort:** M.
- **Bidwright approach:** a page reader that returns text lines with normalized boxes and flags vector-text pages, compacted to `text @x,y` for the model.
- **Our design:** new `app/services/plans_reader_pagefacts.py`, pure functions over pdf bytes with pypdfium2 under `pdf_split._PDFIUM_LOCK`. Positioned lines normalized for `/Rotate` and CropBox (the probe found negative coordinates without this). SHX annotation strings harvested via pypdf. Page classes `cad_text`, `shx_mixed`, `vector_text`, `scanned`, `scanned_ocr`. A `textclean` step drops invisible (render mode 3 where not OCR), off-crop and hidden-OCG text, so injected instructions in hidden text never reach a model. Runs as its own deterministic per-document step outside the 600 s claim, cached by document sha256 + page + extractor_version, stored as JSON in plans-reader storage.
- **Different:** no PyMuPDF; deterministic uses first, model use gated (4.2.3).
- **Value:** exact tags, circuit numbers, note text and schedule cells. **Cost:** extraction $0.
- **Risks:** prompt injection through text (mitigated by textclean plus the existing data framing); extraction time on dense 48x36 sets (p90 tracked). **Success metric:** extraction p90 within budget, never blocks other runs.
- **Critic correction:** the fit analysis proposed a `plans_reader_page_facts` table. v1 keeps JSON in storage; a table and tsvector come only when cross-run queries or search need them. One migration and its RLS saved.

#### 4.2.2 Deterministic uses of page facts (on by default)
- **Verdict:** adopt. **Phase:** P2. **Effort:** S each.
- **Our design:**
  - Title-block sheet numbers, titles, revision numbers and dates for labeled sets. This closes the "labeled sets get role `other`" gap (PLANS_READER.md 8.4) and adds supersession detection (two copies of E-201, different revision dates).
  - Legend and notes regions found from heading text plus `page_role`.
  - **Legend Phase A:** legend regions sent as native-ppi crops (200 to 260 ppi) in the ledger instead of whole sheets at about 44 ppi. A 10x14 inch legend block at 220 ppi is about 3.6k tokens. No new group kind.
  - **Keyed, sheet and general notes** parsed into `{number, text}` per sheet with callout counts, sent verbatim to the sub for that page.
  - **Spec coverage p2:** Division 26/27/28 section headers from the spec text layer, each marked present, delivered and cited.
- **Value:** fixes the 44 ppi legend, makes scope-changing notes exact. **Cost:** legend crops about -$0.5 to +$0.3; keyed notes +$0.1 to $0.3.
- **Success metric:** sheet number and title at least 95% correct on labeled sets; legend crops cause no regression on smoke.

#### 4.2.3 Text layer inside sub calls (gated)
- **Verdict:** adopt, default off. **Phase:** P2. **Effort:** S.
- **Our design:** setting `plans_reader_text_layer_in_subs`. When on, `_add_document_pages` adds an `input_text` part per page in compact `text @x,y` form (title-block lines deduped, capped near 400 lines), and `plan_page` counts the tokens. SUB_SYSTEM gains a short "TEXT LAYER" paragraph: exact for tags and note text, never contains symbols, may be missing.
- **Model and cost:** +$1.5 to $2.5 per run (about 3 to 4k tokens per sheet plus overhead).
- **Gate:** a core A/B, on vs off. Enable only with at least +3 reading-class lines and no kind worse than noise; the owner decides on the extra spend (decision 7).

#### 4.2.4 Render and effort experiment
- **Verdict:** adapt. **Phase:** P2. **Effort:** S.
- **Our design:** one decision-grade experiment with flags we already have: adaptive vs tiles_260, and medium vs high effort, on 3 to 4 dense TI and 48x36 cases. Report per kind, so the owner can choose per category (for example tiles for power and LV plans).
- **Different:** Bidwright's 1568 px cap is a provider limit; we keep our exact patch math. Not a full matrix.
- **Cost:** about $100 inside the P2 A/B budget.

#### 4.2.5 Schedule hybrid: panels first, code validates and merges
- **Verdict:** adapt. **Phase:** P3. **Effort:** L.
- **Prerequisite:** answer from the 66 golden BOQs whether G3 lists breakers as BOQ rows for every panel, only new panels, or only spares (decision 8).
- **Bidwright approach:** a generic table census with separate, non-additive observations. The non-additive rule is the part worth keeping.
- **Our design:** `plans_reader_schedules.py`. A region finder from heading text (PANEL, LIGHTING FIXTURE SCHEDULE, EQUIPMENT CONNECTION, FEEDER). Text-assisted column mapping by the model (the model names columns, code reads cells). Deterministic panel validators: circuit continuity, odd left and even right, poles vs spaces consumed, SPARE vs SPACE, `(E)` existing flags. Code merges validated schedules; the model only confirms or corrects by `schedule_id`. `SCHEDULE_ITEM` (`plans_reader_schemas.py:263`) gains required-nullable `source` (`text_layer | vision`) and `parse_confidence`. Scanned and SHX-only schedules fall back to today's vision transcription.
- **Different:** electrical semantics (circuits, poles, trip, AIC, MCA/MOCP), not generic tables.
- **Value:** breakers, feeders, fixtures; fewer output tokens than vision transcription. **Cost:** about -$0.5 to +$0.3.
- **Risks:** messy schedules; report unparsed rows, never guess. **Success metric:** schedule cells at least 98% correct on 10 sheets from at least 6 engineers.

#### 4.2.6 Narrowed electrical census
- **Verdict:** adapt. **Phase:** P3. **Effort:** M.
- **Our design:** `plans_reader_census.py` over validated schedules, producing observations that are never added to plan counts: fixture type scheduled but never counted; plan tag not in the schedule; equipment with MCA/MOCP and no disconnect row; panels with no schedule (into `missing_documents`). Contradictions go into the main prompt as facts to reconcile, and main must cite which value it used. Panel breaker counting stays behind a measured new-vs-existing signal, because TI panels mix existing and new circuits.
- **Value:** the highest-recall check for breakers, disconnects and equipment connections (lighting already matched 13 of 13). **Cost:** $0.
- **Success metric:** census precision at least 60% against estimator BOQs.

#### 4.2.7 Text-tag occurrence floors
- **Verdict:** adopt. **Phase:** P3. **Effort:** S.
- **Our design:** count fixture type marks and device annotations (GFI, WP, EV) in plan-view regions only, excluding title block, legend and schedule regions. Stored as observations with method `text_tag`. A text count is a floor (engineers tag one per group with "TYP"), and "none found" means unknown, never 0.
- **Cost:** $0. **Risks:** real precision unmeasured until P3.

#### 4.2.8 Pre-emptive small-text tiles
- **Verdict:** adapt. **Phase:** P3. **Effort:** M.
- **Our design:** where text height at the planned ppi is under about 7 px (dense panel blocks), `pack.plan_page` (`plans_reader_pack.py:120`) adds a 220 to 260 ppi region tile in pass 1 instead of waiting for the model to flag it. Per-run cap on pre-emptive tiles.
- **Value:** sharp schedules without 260 ppi everywhere. **Cost:** inside the P3 band.

#### 4.2.9 Scanned and OCR'd pages: probe, estimate fix, badge
- **Verdict:** adapt routing, skip external OCR. **Phase:** P1. **Effort:** S.
- **Our design:** a per-page pdfium probe (chars, a full-page image object, render-mode-3 text) in both planning loops. Scanned and OCR'd pages are priced from pixel size, fixing the 1,100 vs about 8,400 token estimate. An extraction badge per document in DocumentTree and the brief (`cad_text`, `vector_text`, `scanned`).
- **Different:** Bidwright routes through Azure DI. Only about 5% of our pages are scans and vision already reads them.
- **Success metric:** pre-run estimate error under 15% on scanned sets.

#### 4.2.10 Later and skip in this cluster

| Capability | Verdict | Reason and trigger |
|---|---|---|
| Legend group kind (Phase B: structured symbol dictionary, glyph strip) | later | Only if Phase A crops leave decoding inconsistent across groups |
| Symbol matching (OpenCV or vector signature) | later | 2-day spike first: rotation-aware, 5 golden projects, adopt only at precision 0.95 and recall 0.8 on 3+ device types. Vector signatures (same path clusters as the legend glyph) may beat pixels on CAD PDFs; OCG layers are a variant |
| Bounded vector extraction | later | Fold into schedules (ruling lines) only if text clustering is not enough |
| OCG layer use | later | Script-only spike in P2; about 20% of files carry layers |
| Page-text search and `search_documents` tools | later | Easy on top of P2 facts |
| Azure DI or other external OCR | skip | About 5% scans; adds a data vendor and credentials |
| DXF/DWG takeoff | skip, with trigger | Zero files; revisit at 3+ CAD bids per quarter (ezdxf block counts, never DWG) |
| Text-angle upright rendering | skip | Breaks the page-fraction coordinate contract every position uses |

### 4.3 Scale and length measurement

**Measured facts:** 16% of electrical pages carry PDF viewport dictionaries (`/VP` with `/Measure`), a free scale source no research report found. 18 of 41 pages with scale text carry more than one scale, and NTS often sits beside a real plan scale. Estimators measure in Bluebeam (decimal footages such as 16,365.7 FT). 33 trench and saw cut markup PDFs hold estimator `/Line` annotations: free length truth. FT rows are 28% of priced lines.

**Corrections to the research.** Bidwright's fractional scale regex has no mixed-number support (`1-1/2" = 1'-0"` reads as 1/2", a 3x error). Its page-level NTS and multiple-scale flags would fire on nearly every G3 power plan. Its dimension-line scale and route tracing sit behind the same 2,000-path cap, so they mostly return "too dense" on our sheets. It has no electrical length math at all.

#### 4.3.1 Per-viewport scale detection
- **Verdict:** adapt. **Phase:** P4. **Effort:** M.
- **Bidwright approach:** title-block scale regex with a SCALE-proximity bonus, plus conflict flags, per page.
- **Our design:** `app/services/plans_reader_geometry.py` combines four sources per viewport: scale text from the P2 text layer (mixed numbers, NTS, `1:N` only after SCALE, time stamps rejected); `/VP` measure dictionaries with `/Rotate` and CropBox applied, unit ambiguity resolved by snapping to the standard scale list and agreeing with the text; scale bars; and reduced-print checks (page not near 24x36, 30x42, 36x48 or 22x34/34x44, or a "half size" note). The model supplies viewport titles via `SUB_PAGE.viewports[]`; code supplies numbers. Cached by sha256 + page + region.
- **Value:** the root of every length; half-size sets flagged. **Cost:** about +$0.1 output tokens per run.
- **Risks:** a wrong inch-vs-foot resolution is a 12x error; disagreement becomes a conflict, never a silent pick. **Success metric:** at least 95% of scaled viewports correct on 20 hand-labeled projects; all half-size sets flagged.

#### 4.3.2 Scale precedence and conflict guard (calibration v1)
- **Verdict:** adapt. **Phase:** P4. **Effort:** S.
- **Bidwright approach:** region-scoped calibrations, human wins, no physical unit across regions or under conflicting calibrations.
- **Our design:** precedence human > viewport with explicit unit > text agreeing within 2% with another source > model read alone (confidence capped). Over 5% disagreement is a conflict and no length is produced. A multi-module known-object check (2x4 ceiling grid, parking stall, door leaf) only flags. Results live in run JSON; no scales table in v1.
- **Success metric:** zero lengths on NTS or conflicting scales.

#### 4.3.3 Route measurement from structured waypoints
- **Verdict:** adopt. **Phase:** P4. **Effort:** M.
- **Bidwright approach:** measure plus snap, with physical units only from a calibration.
- **Our design:** `RUN_ITEM.route` as structured waypoints (3 decimals) in the frame of the cited image, replacing the `route:` notes text. `measure_route()` converts to sheet fractions, then feet with the selected scale, requires every vertex inside one calibrated viewport, and adds vertical legs from owner conventions as separate terms (decision 9). A run is never measured on both the overall and the enlarged plan. Code builds markup lines from routes; labels print measured FT and the scale used. Lengths recompute after modify. **Side by side first:** the BOQ keeps the model's length with a caveat when the two differ by more than 25%; code values take over per kind only after the eval passes and the owner signs off (decision 10).
- **Value:** the drawn line and the number agree. **Cost:** +$0.2 to $0.5 output tokens.
- **Risks:** waypoints can follow the wrong line; the markup PDF makes that visible. **Success metric:** trench and saw cut within 15% on at least 70% of the N matchable projects, or a clear gain over the model baseline.

#### 4.3.4 Conductor grammar and code-filled FT
- **Verdict:** adopt. **Phase:** P4. **Effort:** M.
- **Our design:** `parse_conductors()` grammar for `4#500 KCMIL CU + 1#2/0 CU G`, `3#12, 1#12G`, `(2) SETS OF ...`, fed by text-layer feeder schedules. Conduit FT = length x sets; conductor FT per size = length x count x sets + makeup per termination (default 0 until the owner sets it). Unparseable strings stay with the model and are flagged. Quantities stay pre-wastage; the renderer keeps adding 10%.
- **Success metric:** zero conductor arithmetic mismatches; feeder rows within 10% where a feeder schedule exists.

#### 4.3.5 Markup annotation reader (eval truth)
- **Verdict:** adapt. **Phase:** P4 week 1. **Effort:** S.
- **Bidwright approach:** a Bluebeam CSV parser that feeds only a viewer.
- **Our design:** `plans_reader_annotations.read(pdf_bytes)` with pypdf over the 33 markup PDFs: subtype, vertices, `/Measure` calibrations (estimator Bluebeam scales become scale labels). Count N, the projects that are matchable and scalable, then record the model-length baseline before any measurement code ships.
- **Cost:** $0.

#### 4.3.6 Branch wire bands v1
- **Verdict:** adopt. **Phase:** P4. **Effort:** S.
- **Our design:** compare the run's #12/#10 FT per device (G3 median 122, IQR 35 to 261) and #12 FT per 3/4" EMT FT (median 2.46, IQR 1.57 to 3.0) with history bands, stratified by project type when n is at least 8. Out-of-band rows get a caveat. Quantity ratios only, no prices.
- **Value:** catches order-of-magnitude misses. **Cost:** $0.

#### 4.3.7 Later and skip in this cluster

| Capability | Verdict | Reason and trigger |
|---|---|---|
| Vector snapping and tracing | later | Only if waypoint drift exceeds 30% of length error |
| Dimension-line scale | later | Civil and site tie-breaker only |
| Branch circuit v2 (rectilinear MST per circuit) | later, XL | Needs scale, device positions and circuit tags |
| Scale table plus human override UI | later | When estimators ask |
| Feet-inches display | skip | G3 BOQs use decimal FT |

### 4.4 Deterministic integrity and coverage

Everything here is Python between model calls we already make. Almost all of it costs $0 per run, and one item saves money.

**Corrections to the research.** Bidwright's finalize check compares structured summary fields, not prose. Its contradictions vocabulary is mechanical (lugs, anchors), with no panel schedules. Delivery is already recorded in our call snapshots (`plans_reader_calls.request_snapshot.parts`); nothing summarizes it.

#### 4.4.1 Output-cap pre-sizing and pass-2 retry
- **Verdict:** adopt. **Phase:** P1. **Effort:** S.
- **Bidwright approach:** detect a cut-off payload and ask for smaller ones.
- **Our design:** planned output per sheet scales with `page_role` (a schedule sheet plans more than a site plan). A sub group that dies on `max_output_tokens` (`LlmBadOutputPermanent`) gets one pass-2 retry at 128k and one lower effort, mirroring the main retry in `_advance_consolidating` (`plans_reader_runs.py:2064`). The run error names only the sheets of failed sub groups.
- **Different:** recovery in code, not a request to the model.
- **Cost:** $2 to $5 only when triggered. **Success metric:** zero sub groups lost to cut-off on the core tier.

#### 4.4.2 LLM roll-up off: code merge of exact duplicates
- **Verdict:** adapt. **Phase:** P1. **Effort:** S.
- **Our design:** setting `plans_reader_llm_rollup`, default off. `_category_output` (`plans_reader_runs.py:1770`) already falls back to `_merge_outputs`; that fallback learns to drop exact duplicates (same page, item key, unit) and keep the stronger basis, never summing across pages. Validate with one Five Star rerun.
- **Value:** -$1 to $4 per multi-group run; removes the 128k roll-up ceiling and the concatenation double count. Its only lost output is a fresh per-category summary.
- **Critic correction:** moved from P3 to P1. The full merge with stable row ids and code-applied escalation patches lands with row ids in P3 (4.5.2).

#### 4.4.3 Unit and house-rule guards
- **Verdict:** adopt. **Phase:** P1. **Effort:** S.
- **Bidwright approach:** UoM equality, explicit conversion whitelist, refuse pixel units on priced rows.
- **Our design:** `check_units(boq)` grounded in the 76 workbooks: unit by item class (FT for conductor, conduit, cable, saw cut, trench; EA for devices, fixtures, breakers; LS only for allowances), whole-number EA, no EA and FT merge of one description. Scale-independent in P1; length guards (no length without a selected scale, run length under 1.5x the viewport diagonal) arrive in P4. **Auto-correct only the unambiguous cases:** the wastage flag on saw cut, trench, bare ground and ground rods (today `boq_quantity`, `plans_reader_render.py:133`, trusts the model), and conduit text normalized to the renderer's `3/4"C EMT` form when the type token is clear (otherwise `conduit_fittings` silently produces no fittings). Every correction is recorded and visible; everything else is flagged. The owner can switch to flag-only (decision 5).
- **Success metric:** zero conduit rows rendered without fittings or a flag.

#### 4.4.4 Output integrity gate (flag-only)
- **Verdict:** adapt. **Phase:** P1. **Effort:** S.
- **Bidwright approach:** refuse a row whose citation is not real or whose arithmetic fails; integrity only, never quality.
- **Our design:** `check_group_output()` in new `plans_reader_checks.py`, called in `run_group` after `_finalize` (`plans_reader_agents.py:1468`). Allowed pages = the group's pages plus the ledger pages actually sent. Checks: cited pages are allowed; `sheet_number` matches the ledger (E-101 vs E101 normalized); finite non-negative quantities; parsable coordinates; `basis = schedule_text` has a schedule row on that page, else downgraded. Mechanical repairs (cover-page off-by-one) are applied and logged; other rows keep their quantity and are flagged "unverifiable reference". The same gate checks main `sheet_refs`.
- **Different:** annotate and repair, never refuse, because a re-ask costs a whole $2 to $6 group.
- **Cost:** $0. **Success metric:** under 1% unverifiable references by P5.

#### 4.4.5 Coverage telemetry
- **Verdict:** adopt. **Phase:** P1 (sheets), P2 (specs). **Effort:** S.
- **Bidwright approach:** read records merged into unread, partly read and read per document; "delivery, not comprehension".
- **Our design:** `build_coverage(run)` from stored data: a sheet grid with delivered, acknowledged, productive, escalated and lost states. Sheets in the cover index but absent, and E-sheets never acknowledged, become `missing_documents` facts. Spec coverage from 4.2.2 in P2. A code-written "Pages reviewed" appendix in the brief, so the model cannot claim coverage it did not have.
- **Value:** answers "did it read E-201". **Cost:** $0.

#### 4.4.6 Deliverable consistency, code re-render and staleness slice
- **Verdict:** adapt. **Phase:** P1. **Effort:** M.
- **Bidwright approach:** compare claimed totals with computed totals at finalize.
- **Our design:** advisory checks: marked-plan dots vs legend quantity; trench and saw cut markup FT vs BOQ FT (and a BOQ trench or saw cut row with no markup, the Five Star case). After a BOQ modify, the estimate is re-rendered by code from the new BOQ (no model call), a stale banner shows on dependents until then, and a code-computed modify diff (added, removed, changed rows) is stored with the version. This closes the BOQ-to-estimate drift in `deliverable_spec` (`plans_reader_runs.py:2536`).
- **Cost:** $0. **Success metric:** zero self-contradicting deliverables on smoke.

#### 4.4.7 Findings as jsonb and a Checks list
- **Verdict:** adapt. **Phase:** P5. **Effort:** S.
- **Our design:** findings from 4.4.3 to 4.4.6, 4.2.6 and 4.4.8 stored as jsonb on the run output (no table yet), shown in a Checks list (new `checks` entry in `RUN_TABS`, `RunView.tsx:86`) grouped by severity with jump-to-sheet links. Dismissals with a reason are eval labels.
- **Critic correction:** the fit analysis proposed a `plans_reader_findings` table and a 0 to 100 score. The table waits until accept/dismiss writes need it; the score is skipped (see 4.4.9).

#### 4.4.8 Tier 1 rule lane
- **Verdict:** adapt. **Phase:** P5. **Effort:** S.
- **Our design:** high-precision $0 checks: wire with no conduit of matching size; underground with no trench; fixture types vs BOQ; duplicate rows across areas with overlapping sheet_refs; MC where the spec says EMT (once spec coverage exists).
- **Success metric:** Estimating Admin judges at least 60% of flags useful.

#### 4.4.9 Later and skip in this cluster

| Capability | Verdict | Reason |
|---|---|---|
| Tier 2 ratio bands (stratified, p10 to p90) | later | After the shared normalizer exists (Track B) |
| Weighted integrity score | skip | Gates nothing; validating its weights would cost about $260 of sweeps |
| Payload shape tolerance | skip | Strict schemas make shape errors impossible |

### 4.5 Evidence and provenance

The cheapest high-leverage cluster. Almost no new model calls: schema fields, deterministic Python and small UI.

**Corrections to the research.** We already have most of an evidence-view registry: `_Parts.image` records a manifest entry per image (label, document, page, ppi, tile box, detail) into the call snapshot. New finding: the escalation prompt asks the model to convert tile positions to sheet fractions itself, a likely source of dot drift. Bidwright stores every crop; we can re-render.

#### 4.5.1 Code-built marked plans and markups (v0 then structured)
- **Verdict:** adopt. **Phase:** P1 (v0), P5 (structured). **Effort:** S.
- **Bidwright approach:** marks are the takeoff, so there is no separate re-drawn output.
- **Our design (P1 v0):** code parses the existing `at (x,y)` and `route:` text from sub rows and draws marked plans and markups directly, one dot per parsed position, instead of the main agent re-transcribing. Trench and saw cut BOQ rows with no route print "route not located". Advisory check: dots vs legend quantity.
- **P5:** switch from the text parser to structured points (4.5.2).
- **Value:** one dot per device; exposes the saw cut miss. **Cost:** slightly negative (main stops emitting dots).
- **Success metric:** at least 0.9 marked-plan dots per parsed position (P1); 0.8 to 0.9 dots per counted device on Five Star and YMCA (P5).

#### 4.5.2 Stable short row ids with likely_supersedes
- **Verdict:** adopt. **Phase:** P3. **Effort:** S.
- **Our design:** code assigns short ids per run when outputs are stored (`c1`, `r1`, `s1` for counts, runs, schedules), never the model. Escalation and revision rows carry a `likely_supersedes` hint; code applies patches by id and keeps the superseded value. Nothing is ever deleted: every input row is in the output or in a superseded or alternates slot (100% row conservation, enforced in tests).
- **Value:** prerequisite for routes, provenance and addenda. **Cost:** a few tokens per row.

#### 4.5.3 BOQ source_refs, reconcile and funnel (conditional)
- **Verdict:** adopt, conditional. **Phase:** P5. **Effort:** L.
- **Bidwright approach:** links from lines to marks with multipliers; the server sums.
- **Our design:** `BOQ_ITEM` (`plans_reader_schemas.py:310`) gains `source_refs: [{row_id, multiplier}]` and `combine: sum | schedule_value | single | none`. `reconcile_boq()` runs in `_finish_with_main` (`plans_reader_runs.py:2192`) before render: refs exist, units compatible, code quantity within 0.5% (FT within max(1 ft, 2%)), statuses `reconciled | mismatch | unsourced | rule_derived`. A funnel per sheet: detected, placed, linked, orphan rows, plus `excluded_rows` (existing to remain, other trade), `schedule_cited` and `assumed_no_view` classes. DRAWING SHEET# derives from resolved refs. Re-run on every BOQ version.
- **Gate:** built only if P1 attribution shows `lost_in_consolidation` plus mis-summed lines are 25% or more of misses. Otherwise P5 shrinks by about 1.5 weeks.
- **Cost:** about +$0.10 to $0.15 output per run. **Risks:** schema ripple across about 470 tests, chat tools, FE types and the modify path; old runs need tolerant reads.
- **Success metric:** at least 90% of device and fixture EA lines sourced, at least 95% reconciled, `assumed_no_view` share reported.

#### 4.5.4 Structured points, code conversion and compliance
- **Verdict:** adapt. **Phase:** P5. **Effort:** M.
- **Our design:** `COUNT_ITEM.points` in the frame of the cited image (0..1 of what the model saw), plus `positions_complete`. Code converts with the clamped tile box and w/h px, removing model coordinate arithmetic. Points within about 0.004 of the sheet across two overlapping tiles are merged, and the count is flagged if the merge changes it. A positions-compliance metric (counted_symbols quantity vs len(points)). The cap rises from 60 to about 250 per row if the owner approves (decision 11).
- **Cost:** +$0.5 to $1.25 output, offset by stripping points from main inputs: net about -$0.25 to +$1.0.

#### 4.5.5 View-label stamping, document sha256, crop route
- **Verdict:** adapt. **Phase:** P5. **Effort:** S.
- **Our design:** rows cite the view label they were shown (`D2 P5 T3`); code resolves it against the call manifest and fills document, page and tile, ignoring the model's own numbers if they disagree. No views table and no stored PNGs: `plans_reader_documents.sha256` plus ppi and box let `GET .../documents/{doc}/pages/{p}/crop?region=` re-render the exact crop, with a "source missing" state.
- **Different:** Bidwright persists every image; ours re-renders and compares hashes.

#### 4.5.6 Annotated full-size PDF, derivation basis chips, humanization
- **Verdict:** adopt (PDF, humanization), adapt (derivation). **Phase:** P5. **Effort:** S each.
- **Our design:** marked plans also rendered as vector annotations on the source sheet with the `render_markup_pdf` machinery (Bluebeam-toggleable; about 60% of a viewer's value at a fraction of the cost). Derivations are built by code from typed refs (no model-written formulas) and shown as basis chips; fitting rows point to their parent conduit line. `humanize()` turns `D2 P5` and row ids into "E-201 Power Plan L2" in brief text, chat answers and xlsx notes. Optional: a small `bid_changers` array on the specifications sub output (SPEC_ITEM has no quote field today), so a later reviewer has quotes to validate.
- **Critic correction:** the fit analysis reserved a `pricing_basis` slot on derivations. Not now; pricing lives in Track B tables.

#### 4.5.7 Later in this cluster

| Capability | Trigger |
|---|---|
| In-app evidence viewer (sheet image with SVG overlay) | 0.8 or more dots per device |
| Estimator-editable marks driving quantities | After the viewer; owner decides who may edit |
| explain_row chat tool | After provenance and the owner's data decision |

### 4.6 Suggested pricing

Section 7 holds the full design. This section rates the capabilities.

**Corrections to the research.** Filling H and I in our estimate would not "compute totals automatically": our writer emits no J to O, subtotal or recap formulas, so a priced variant must write them all. Fixtures are not priced only through quotes: the estimator enters a placeholder material value on 99% of fixture lines. Bidwright's "strong" match label is lexical only (exact text plus a price present), with no sample size, spread or recency. Its UoM whitelist lacks C and M. The G3 labor index per project has an IQR of 0.93 to 1.03, so item units are stable; the big uncertainty is the project-level 1.43x uplift.

| # | Capability | Verdict | Phase | Effort | Our design in one line | Cost |
|---|---|---|---|---|---|---|
| 1 | Pricing security boundary | adopt | B1 | S | `require_pricing_view`, `AI_PRICING_ENABLED`, forced RLS, route tests, 404 on download and preview, INTERNAL watermark (7.8) | $0 |
| 2 | Shared normalizer and attribute matching | adapt | B1 | M | `electrical_items.py` promoted from the golden scorer; family gate then hard attribute rejects then rapidfuzz; fixture tags excluded from cross-project identity (7.2) | $0 |
| 3 | Cost history library | adapt | B1 | M | `cost_sources`, `cost_observations`, `cost_items`; benchmarks computed on the fly (7.1, 7.3) | $0 |
| 4 | Statistical quality labels | adapt | B1 | S | strong, good, weak, missing from n, spread and recency; fixture and gear material capped at weak (7.4) | $0 |
| 5 | Pricing-match LOPO eval | adopt | B1 | M | leave-one-project-out over a 300-pair gold set; the gate for every suggestion (7.6) | $0 |
| 6 | Estimator estimate audit | adopt | B1 | S | flags unit outliers and quantity deltas on sealed rounds before Verify and auto send (7.7) | $0 |
| 7 | Correction capture | adopt | B1 | M | 4.1.5, same hook | $0 |
| 8 | Automatic backtest | adopt | B2 | S | suggestions vs the estimator's actual units on every new sealed round | $0 |
| 9 | Pricing tab and suggested xlsx | adapt | B2 | M | read-only, outside `DELIVERABLE_KINDS`, side columns S to X, H and I blank (7.5) | $0 |
| 10 | Early budget range (ROM) | adopt | B2 | S | a range at intake from AI quantities times benchmarks, clearly labeled | $0 |
| 11 | G3 labor adjustment | adapt | B2 | S | dollar range by wage_type, never hours, never in a unit cell (7.4) | $0 |
| 12 | Risk and sensitivity drivers | later | - | S | needs pricing and per-row basis | $0 |
| 13 | NECA condition factors | later | - | M | `labor_breakdown` is empty in all 50 reviews; no G3 factor data | $0 |
| 14 | Parametric assemblies | later | - | L | G3 prices a device as one row; expanding kits would double count labor | $0 |
| 15 | Vendor quote line extraction | later | - | L | quotes arrive at bid time; table-first when built; about $0.01 to $0.03 per quote | small |
| 16 | Rate books, customer pricing ladder | skip | - | - | labor rate is one constant; BDR already owns markups, Verify and proposals | - |

**What we do differently from Bidwright:** medians and IQR, not an untrimmed weighted average; labor units not recency-decayed (productivity is stable), material recency-weighted with a longer half-life and copper flagged as volatile; conversions explicit with a stored fact string; quality labels statistical, not lexical; the G3 adjustment is a separate visible layer.

**Critic corrections:** AI-run suggestions do not ship while takeoff recall is around 30%. They are built in B2 but stay hidden behind `AI_PRICING_ENABLED` and are shown per kind only when LOPO meets the owner's thresholds and core-tier takeoff recall for that kind meets an owner threshold (decision 16). The value-first draft's Phase 2 pricing was rejected for this reason. The fit analysis's `plans_reader_price_suggestions` table with accept/override decisions and the `estimate_suggested` deliverable kind are replaced by a lazily computed tab and a dedicated gated route, so no pricing kind ever enters `DELIVERABLE_KINDS` or the chat tool enums.

### 4.7 Independent review loop

Bidwright's review loop fits our pipeline shape directly (new group kinds after main). The merged roadmap still defers it, because the deterministic checks of 4.4 and 4.5 deliver most of its value first at $0, and a reviewer needs positions, region evidence and patch ops to be precise.

**Corrections to the research.** The revision prompt lives in `estimate-review-loop.ts`, not the facts module. Bidwright's resolve loop catches and logs each failed action, then marks the recommendation resolved anyway. Sensitivity drivers are library code nothing calls. We already have a text-only Anthropic call (`llm.py:681 _anthropic_call`), but a Claude reviewer needs a parts transport with images and structured outputs.

| Capability | Verdict | Design when picked up | Cost estimate | Trigger |
|---|---|---|---|---|
| Independent text reviewer | later | `review` group kind after main, read-only by construction (returns only its own row), facts block computed in code, `REVIEW_SCHEMA` with coverage, findings and questions; propose mode only | $1.5 to $3 on Astra | Next horizon after P5; on Astra first |
| Review spot recount (blind) | later | code picks 8 to 12 low-confidence or high-quantity targets, crops at 260 ppi, the model counts without seeing our numbers, code compares | $0.4 to $1.6 | Needs positions and region evidence (P5) |
| Spec coverage checklist YES/VERIFY/NO | later | `COVERAGE_ITEM` with a validated `spec_ref {doc, page, quote}`; coverage percent computed in code | inside the reviewer | Seed with `bid_changers` (P5) |
| Revision turn with dispositions | later | fresh call in propose mode (not a chained response), every finding gets addressed or declined, ops never a full spec | about $1 | After typed patch ops |
| Typed patch ops and one-click apply | later | ops with `from` preconditions, dry-run preview, all-or-nothing under `claim_change_slot`, BOQ and estimate written together, staleness by row hash | $0 | First piece of the reviewer horizon |
| Review panel | later | Checks list (4.4.7) covers the MVP | $0 | With the reviewer |
| Questions inbox (askUser) | later | region-anchored questions with `default_assumption`; the run never waits; light version through the modify path first | about $1 per apply | After patch ops |

**What we will do differently when built:** coverage percentages and money impacts computed in code, never model strings; apply with preview and a transaction; the reviewer can be a different provider only after the owner's vendor decision (decision 30).

### 4.8 Runtime, providers, spend and revisions

**Corrections to the research.** The splitter spend leak has two causes: no Anthropic prices, and `ai_spend` sums only `plans_reader_calls` while the splitter writes to `llm_call_log`. `_anthropic_usage` folds cache-write tokens into input at 1x, which would under-count Anthropic caching. Bidwright has no PDF sheet-to-sheet revision diff; its PDF staleness is per whole document. Its Bluebeam import feeds only a viewer.

#### 4.8.1 Spend control
- **Verdict:** adopt. **Phase:** P1 (prices, ceiling), P2 (ledger view). **Effort:** S.
- **Bidwright approach:** effectively none (turn caps and a watchdog). We are ahead; this closes our own leaks.
- **Our design:** verified `llm_pricing` rows for `claude-opus-5` and `gpt-5.4-mini` (the splitter models) with check dates; `cache_write` multipliers on `ModelPrice` and a `cache_write_tokens` field on `LlmUsage`; a per-run ceiling that moves a run to `over_cap` once its cost passes 1.25x `cost_usd_max`. In P2 (migration M1) an `ai_spend_ledger` view unions `plans_reader_calls` with splitter `llm_call_log` rows for plans_reader split jobs, linked through `llm_jobs.target_id` or a new nullable link column.
- **Critic correction:** cap reservations (to close parallel overshoot) are dropped; the per-run ceiling plus the pre-call check is enough at concurrency 2.
- **Success metric:** splitter spend counted; no run exceeds 1.25x its worst-case estimate.

#### 4.8.2 Chat response compaction
- **Verdict:** adopt. **Phase:** P1. **Effort:** S.
- **Bidwright approach:** paginated tool output with explicit `omitted` and `next_offset`, after a measured run built 200k tokens of tool output.
- **Our design:** `get_output` gains filters (section, sheet, page, material category) and paging (default 40); `get_deliverable` returns BOQ rows as one-line strings; per-tool cap drops from 200k to 40k chars and per-round from 300k to 80k (`plans_reader_chat.py:91-95`); every truncation says what was left out.
- **Value:** saves $0.10 to $0.60 per heavy message. **Cost:** negative.

#### 4.8.3 Flex economy run and prompt-cache prefix
- **Verdict:** adopt (Flex), optional (cache key). **Phase:** P1. **Effort:** S.
- **Our design:** day 1, confirm that gpt-6-astra supports Flex and what data retention it requires. If yes, an "Economy (slower)" option on New Run and for eval sweeps, about 50% off with no new vendor. Optionally, a stable ledger-first prefix with a `prompt_cache_key` per run, measured before it stays.
- **Risks:** Flex latency and capacity errors; retention terms are an owner decision (30).

#### 4.8.4 Addenda revision runs and the delta workbook
- **Verdict:** adapt. **Phase:** P6. **Effort:** L.
- **Bidwright approach:** estimate rows paired on a natural key, duplicates paired in order; document-level staleness.
- **Our design:** a "Revision of run X" option. Reissued sheets pair with the parent by splitter sheet number, with title and a perceptual hash as fallbacks and estimator confirmation. Only paired-changed and new sheets are re-read, with the parent's rows for that sheet in the prompt (the escalation pattern); other rows carry forward with `carried_from`. Narrative addenda produce `addendum_changes[]` (add, delete, revise, sheet, verbatim quote). Main runs in patch mode. A deterministic per-sheet row diff renders "ADDENDUM N DELTA.xlsx" in house style (breakers, fixtures by mark, receptacles by type, trench and saw cut FT shown separately). Pricing deltas only behind the pricing gate. The parent shows "Superseded". BDR may suggest a revision run when the splitter classifies an addendum (decision 25). Migration M4: `runs.parent_run_id`, `revision_label`, `plans_reader_files.sha256`, deliverable kind CHECK plus `revision_delta`.
- **Value:** the highest-value single item for estimators during the bid window. **Model and cost:** $5 to $8 per typical addendum vs about $26 for a full re-run.
- **Risks:** carry-forward inherits parent errors (the delta says which rows were carried; a full re-run stays one click away); a hash can miss a tiny moved cloud (estimator can force a re-read).
- **Success metric:** at least 85% of the estimator's changed lines on the 16 addendum projects, under 10% false deltas, revision cost at most 30% of a full run.

#### 4.8.5 Revision compare tab and pixel overlay
- **Verdict:** adapt. **Phase:** P6. **Effort:** M.
- **Our design:** a Revision tab with a sheet table (unchanged, changed, new, removed), the BOQ delta and, after alignment with the title block masked, a $0 pixel overlay (red added ink, blue removed) at 100 ppi.
- **Value:** useful even without AI.

#### 4.8.6 Later and skip in this cluster

| Capability | Verdict | Reason and trigger |
|---|---|---|
| Anthropic parts transport (images, structured outputs, caching) | later, L | Vendor approval, 32 MB request cap, tile-seam risk (Claude needs about 9 tiles for a 42x30 sheet at our ppi). Estimate: Opus-class about $11, Sonnet-class about $5.50 per Five Star run (prices unverified) |
| Per-step model routing | later, M | Subs dominate cost; routing main alone saves little |
| Two-step main and 1M-context main | later, M | P1 to P5 shrink main input; P3 adds size guards |
| Pay-once background requests, deploy drain | later, M | Measure the double-billing incident rate first |
| Stage spine (phase stepper) | later, S | Downstream phases do not exist yet |
| Bluebeam CSV summary import | later | No summaries exist in the backup; the annotation reader (4.3.5) covers our files |
| CLI-agent harness | skip | Loses strict schemas, the cap and accounting; adds a shell-capable agent next to service keys; would replace the pipeline the owner likes |
| 3D/BIM/CAD editors | skip | Zero files, forks of about 470k lines |

### 4.9 Knowledge, context and tools

**Corrections to the research.** Mode-based chat access is partial, not missing: the change-word regex, writer role, done status and change slot already act as a write mode. Provider-neutral chat history already exists. The compaction gap is specific to `get_output` and `get_deliverable`. Bidwright's hybrid retrieval was lexical in practice. Its seeded electrical persona is generic industrial content.

#### 4.9.1 Commonly-missed constant (mined, holdout excluded)
- **Verdict:** adapt. **Phase:** P3. **Effort:** S.
- **Bidwright approach:** an editable estimator persona (playbook) injected into every briefing.
- **Our design:** for now a code constant mined at $0 from the non-holdout golden BOQs: items estimators list that the AI often misses (grounding electrode system, bonding jumpers, panel directories, mechanical disconnects and whips, exit and emergency circuits, temp power). Estimator NOTE rows and the 48 `proposal_drafts` scope lines seed it if the owner permits. Gated on smoke. Format conventions (breakers as rows, receptacles by type, FT by size) stay in code permanently.
- **Critic correction:** the fit analysis proposed an admin-editable versioned `ai_estimating_playbooks` table and Settings editor now. Deferred until estimators ask to edit rules; a playbook before any measurement exists would be tuned blind.
- **Cost:** about +$0.1 per run. **Success metric:** convention-class misses down on core with no kind worse.

#### 4.9.2 Later in this cluster

| Capability | Reason and trigger |
|---|---|
| G3 playbook table and Settings UI | Code constant first; when estimators ask to edit |
| Knowledge library (standards pages, books) | Pages as playbook sections later; books need licenses (NECA, NEC); extract locally, send only cited passages |
| Typed reference datasets | A `render_rules` JSON (fittings ratios, wastage) only if asked |
| Chat modes, tool registry, per-mode briefings | Needed when pricing chat arrives (unknown tools default to mutating) |
| Region chat and recount | Needs a shared sheet viewer; recounts would propose, never write |
| explain_row and pricing lookup tools | After provenance and the owner's decision on cost data reaching a provider (decision 21) |
| Progressive tool disclosure | skip: we have 7 to 15 tools; the discovery round trip costs more than it saves |

## 5. Ranked capability table

All 99 capabilities, duplicates across clusters merged. Ranking is by the build order the owner should follow if everything recommended is approved. "P" is a phase, "B" is Track B.

| # | Capability | Verdict | Phase | Effort | Why (one line) |
|---|---|---|---|---|---|
| 1 | Golden harness (manifest, blind guard, holdout, runner) | adapt | P1 | S | Every later claim needs a baseline |
| 2 | Per-kind, aggregate and dollar-weighted scoring | adopt | P1 | M | Separates counting error from row style |
| 3 | Stage-wise attribution and miss classification | adopt | P1 | S | Decides reading vs consolidation work, $0 |
| 4 | Output-cap pre-sizing and pass-2 retry | adopt | P1 | S | Recovers dense schedule sheets lost today |
| 5 | LLM roll-up off (code merge, exact duplicates only) | adapt | P1 | S | -$1 to $4 per run, removes the 128k ceiling |
| 6 | Unit and house-rule guards (scale-independent) | adopt | P1 | S | Stops silent missing fittings and wrong wastage |
| 7 | Sheet geometry in page labels | adopt | P1 | S | The model cannot scale without sheet inches and ppi |
| 8 | Scanned/OCR probe, estimate fix, badge (no OCR vendor) | adapt | P1 | S | Fixes the 1,100 vs about 8,400 token estimate |
| 9 | Output integrity gate (flag-only) | adapt | P1 | S | Catches fabricated references at $0 |
| 10 | Coverage telemetry p1 | adopt | P1 | S | Answers "did it read E-201" |
| 11 | Code-built marked plans and markups | adopt | P1/P5 | S | One dot per device; exposes the saw cut miss |
| 12 | Deliverable consistency, estimate re-render, staleness slice | adapt | P1 | M | Removes self-contradicting deliverables |
| 13 | Spend control (price rows, cache writes, ledger view, run ceiling; no reservations) | adopt | P1/P2 | S | Makes the $75 cap truthful |
| 14 | Chat response compaction | adopt | P1 | S | Saves $0.10 to $0.60 per heavy message |
| 15 | OpenAI Flex economy run | adopt | P1 | S | About 50% off with no new vendor |
| 16 | Positioned text layer, SHX, page classes, sanitization | adopt | P2 | M | 77% of pages carry exact text |
| 17 | Keyed notes and callout counts | adopt | P2 | S | Scope-changing text read exactly |
| 18 | Legend Phase A native-ppi crops | adapt | P2 | S | Fixes 44 ppi legends with no new kind |
| 19 | Spec section coverage (Div 26/27/28) | adopt | P2 | S | Proves sections were delivered and cited |
| 20 | Render and effort experiment (existing flags) | adapt | P2 | S | One decision-grade experiment, not a matrix |
| 21 | Schedule hybrid (panels first, code validate and merge) | adapt | P3 | L | Breakers, feeders, fixtures; fewer output tokens |
| 22 | Narrowed electrical census | adapt | P3 | M | Highest-recall check for breakers and disconnects |
| 23 | Text-tag occurrence floors | adopt | P3 | S | Free count floor; zero means unknown |
| 24 | Pre-emptive small-text tiles | adapt | P3 | M | Sharp schedules without full 260 ppi |
| 25 | Stable short row ids and likely_supersedes | adopt | P3 | S | Prerequisite for routes, provenance, addenda |
| 26 | Commonly-missed constant (mined, holdout excluded) | adapt | P3 | S | $0 convention fix |
| 27 | Conductor grammar and code-filled FT | adopt | P4 | M | Removes feeder arithmetic errors |
| 28 | Per-viewport scale detection | adapt | P4 | M | Root of every length; half-size flagged |
| 29 | Scale precedence and conflict guard (calibration v1) | adapt | P4 | S | No length on NTS or conflicting scales |
| 30 | Route measurement from structured waypoints | adopt | P4 | M | Drawn line and number agree |
| 31 | Markup annotation reader (eval truth) | adapt | P4 | S | Trench length truth at $0 |
| 32 | Branch wire bands v1 | adopt | P4 | S | Catches order-of-magnitude misses |
| 33 | Structured points, code conversion, compliance | adapt | P5 | M | Removes tile-to-sheet drift |
| 34 | View-label stamping, document sha256, crop route | adapt | P5 | S | Crop behind each count, no new table |
| 35 | BOQ source_refs, reconcile and funnel | adopt (conditional) | P5 | L | Only if attribution shows consolidation loss |
| 36 | Derivation ledger (folded into reconcile) | adapt | P5 | S | Basis chips; no pricing slot yet |
| 37 | Annotated full-size PDF | adopt | P5 | S | About 60% of the viewer's value |
| 38 | Findings jsonb and Checks list | adapt | P5 | S | Ranked "check first" list |
| 39 | Tier 1 rule lane (duplicates, wire/conduit, UG/trench) | adapt | P5 | S | High-precision $0 checks |
| 40 | Provenance humanization | adopt | P5 | S | Estimators read sheet names, not ids |
| 41 | Pricing security boundary | adopt | B1 | S | Must exist before any cost table |
| 42 | Shared normalizer and attribute matching | adapt | B1 | M | A wrong-size match is worse than none |
| 43 | Cost history library | adapt | B1 | M | 3,422 lines, deterministic |
| 44 | Estimator estimate audit | adopt | B1 | S | $0, recall-independent, protects auto send (0159) |
| 45 | Pricing-match LOPO eval | adopt | B1 | M | Gate for every suggestion |
| 46 | Statistical quality labels | adapt | B1 | S | Adds n, spread and recency |
| 47 | Correction capture (feedback) | adopt | B1 | M | Free row-level eval data |
| 48 | Automatic backtest | adopt | B2 | S | Replaces clicks nobody will make |
| 49 | Pricing tab and suggested xlsx (outside DELIVERABLE_KINDS) | adapt | B2 | M | Internal, side columns, gated |
| 50 | Early budget range (ROM) | adopt | B2 | S | Realistic early consumer of benchmarks |
| 51 | G3 labor adjustment (dollar range) | adapt | B2 | S | Dollars over dollars, never hours |
| 52 | Addenda revision runs and delta workbook | adapt | P6 | L | $5 to $8 vs $26; changed lines only |
| 53 | Revision compare tab and pixel overlay | adapt | P6 | M | Useful even without AI |
| 54 | Bias report and owner-approved rules | adapt | P6 | S | Lessons without heavy machinery |
| 55 | Accuracy UI tab / regression dashboard | later | - | S | Runner reports suffice until 5+ sweeps |
| 56 | Full model and config matrix | later | - | S | Waits for a second transport |
| 57 | Anthropic parts transport | later | - | L | Vendor approval, 32 MB cap, tile-seam risk |
| 58 | Per-step routing | later | - | M | Subs dominate cost; main-only routing saves little |
| 59 | Two-step main / 1M-context main | later | - | M | P1 to P5 shrink main; size guard in P3 |
| 60 | Pay-once background requests, deploy drain | later | - | M | Measure the incident rate first |
| 61 | Independent text reviewer | later | - | L | Next horizon, on Astra, propose mode |
| 62 | Review spot recount | later | - | L | Needs positions and region evidence |
| 63 | Spec coverage checklist (reviewer) | later | - | M | Seed with optional bid_changers (P5) |
| 64 | Revision turn with dispositions | later | - | M | Fresh call, propose mode, after patch ops |
| 65 | Typed patch ops and one-click apply | later | - | M | First piece of the reviewer horizon |
| 66 | Review panel | later | - | M | Checks list covers the MVP |
| 67 | Risk and sensitivity drivers | later | - | S | Needs pricing and per-row basis |
| 68 | askUser questions inbox | later | - | S | Light version via the modify path afterwards |
| 69 | Tier 2 ratio bands (stratified) | later | - | S | After the shared normalizer exists |
| 70 | G3 playbook table and UI | later | - | M | Code constant first |
| 71 | Knowledge library (standards pages, books) | later | - | S | Pages as playbook sections; books need licenses |
| 72 | Typed reference datasets | later | - | S | A render_rules JSON only if asked |
| 73 | Page-text search and search_documents tools | later | - | M | Easy on top of Phase 2 facts |
| 74 | Chat modes, tool registry, briefings | later | - | S | Needed when pricing chat arrives |
| 75 | Region chat and recount | later | - | L | Needs a shared sheet viewer |
| 76 | explain_row and pricing lookup tools | later | - | M | After provenance and the owner data decision |
| 77 | Legend group kind (Phase B) | later | - | M | Only if crops leave decoding inconsistent |
| 78 | Symbol matching (OpenCV / vector signature) | later | - | L | 2-day spike first; OCG variant |
| 79 | Bounded vector extraction | later | - | S | Fold into schedules only if needed |
| 80 | Vector snapping and tracing | later | - | L | Only if drift exceeds 30% of length error |
| 81 | Dimension-line scale | later | - | M | Civil and site tie-breaker only |
| 82 | Branch circuit v2 (MST) | later | - | XL | Needs scale, positions, circuit tags |
| 83 | Scale table and human override UI | later | - | M | When estimators ask |
| 84 | In-app evidence viewer | later | - | L | Gate at 0.8 or more dots per device |
| 85 | Estimator-editable marks | later | - | L | After the viewer |
| 86 | Calibration lessons engine (LLM drafting, A/B) | later | - | M | No paired data yet |
| 87 | Spatial placement scoring and probes | later | - | M | Marked plans are flattened; probes at $0 from outputs |
| 88 | Per-provider resolution probe | later | - | S | OpenAI-only until a transport exists |
| 89 | Stage spine / phase stepper | later | - | S | Downstream phases do not exist |
| 90 | NECA condition factors | later | - | M | labor_breakdown empty in all 50 reviews |
| 91 | Parametric assemblies | later | - | L | Would double count device labor |
| 92 | Vendor quote line extraction | later | - | L | Quotes arrive at bid time; table-first when built |
| 93 | Weighted integrity score | skip | - | - | Gates nothing; costs about $260 to validate |
| 94 | Progressive tool disclosure | skip | - | - | 7 to 15 tools; the round trip costs more |
| 95 | CLI-agent harness | skip | - | - | Loses schemas, cap and accounting; adds shell risk |
| 96 | Azure DI / external OCR | skip | - | - | About 5% scans; adds a data vendor |
| 97 | DXF/DWG takeoff | skip (trigger) | - | - | Zero files; revisit at 3+ bids per quarter |
| 98 | 3D/BIM/CAD editors | skip | - | - | Zero files, huge forks |
| 99 | Text-angle upright rendering | skip | - | - | Breaks fraction coordinate contracts |

## 6. Recommended roadmap

**Shape.** Accuracy first, with the cheap visible wins pulled forward. Deterministic code ships before new model inputs. New model inputs default off until an A/B passes. Code values appear beside model values before they replace them. A parallel Track B builds the price reference and correction capture from week 5, because none of it depends on AI recall.

The three drafts were scored on estimator value, accuracy gain (weighted 1.5x, because recall limits the value of everything else), risk, cost discipline and fit. Accuracy-first scored 8.1, risk-first 7.9, value-first 6.9. The plan below is accuracy-first, plus risk-first's zero-model-call fixes in Phase 1 and its default-off gating, plus value-first's estimator estimate audit (as Track B) and addenda (as Phase 6).

### 6.1 Release gating

- Every phase builds behind settings and lands on dev first (`AI_ESTIMATING_STATE=dev` hides the page from every account on dev until the developer flips it), then staging, then prod at `AI_ESTIMATING_STATE=test` (Executive and IT Admin only).
- New model inputs (`plans_reader_text_layer_in_subs`, code lengths replacing model lengths, pre-emptive tiles beyond the cap) ship default off in prod and are switched on per owner decision after the core A/B.
- `plans_reader_llm_rollup` ships default off (decision 4).
- Pricing surfaces sit behind `AI_PRICING_ENABLED` (default off) and `require_pricing_view`, independent of `AI_ESTIMATING_STATE`. Moving the state to `live` admits the Accountant (`INTERNAL_ROLES`), so the pricing gate must already exclude that role.
- Moving `AI_ESTIMATING_STATE` from test to live needs owner criteria (decision 27). Proposed default: core-tier exact recall at least 50% with dollar-weighted error under 30%, zero blind-guard violations, two consecutive phase exits with no kind worse than noise.

### 6.2 Phase 1: measure, then no-regret fixes (weeks 1 to 4)

**Goal.** A blind, stratified baseline with a known noise band and a cause for every miss. Only zero-risk fixes, each checked against the harness.

**1a. Measure (weeks 1 to 2).** Harness (4.1.1), scoring with the per-line estimate parser (4.1.2), attribution and miss classes (4.1.3), noise floor, estimator noise ceiling, splitter eval, CI replay and backtests (4.1.4), per-kind cost split from `plans_reader_calls`. Day 1: confirm gpt-6-astra Flex support and its retention terms.

**1b. Fixes (weeks 2 to 4).**
- Run reliability: lost sheets named only for failed sub groups; planned output scaled by page_role; one pass-2 retry for a cut-off sub group (4.4.1); `plans_reader_llm_rollup` default off (4.4.2).
- Page inputs: sheet inches, render ppi and feet per page width (at a stated scale, when known) added to page and tile labels; the per-page pdfium probe in both planning loops; scanned pages priced from pixel size; extraction badge (4.2.9).
- Guards: scale-independent unit and house-rule guards (4.4.3); flag-only integrity gate (4.4.4); coverage p1 (4.4.5).
- Deliverables: marked plans v0 and markups from parsed text (4.5.1); advisory dot and markup FT checks; code-only estimate re-render after a BOQ modify, a stale banner and a modify diff (4.4.6).
- Cost: chat compaction (4.8.2); spend truthfulness (4.8.1, price rows, cache-write multipliers, 1.25x run ceiling); optional measured cache prefix and Flex (4.8.3).

**Exit criteria.**
- Core-12 baseline published per kind (exact, aggregate, dollar-weighted) with a documented noise band.
- 100% of Five Star and YMCA misses classified by cause and stage.
- Zero blind-guard violations.
- Smoke: no regression beyond noise, lower cost on multi-group sets.
- Zero sub groups lost to cut-off on the core tier.
- Pre-run estimate error under 15% on scanned sets.
- At least 0.9 marked-plan dots per parsed position.
- Zero conduit rows rendered without fittings or a flag.

**Cost per run:** -$1 to -$4 on multi-group sets; retries $2 to $5 only when triggered; Flex about 50% off. **One-time eval:** about $350 to $450 (about half on Flex). **Migrations:** none.

### 6.3 Phase 2: native text-layer foundation (weeks 5 to 8)

**Goal.** Exact text; deterministic uses first; model use gated.

- Extraction: `plans_reader_pagefacts.py` (4.2.1), its own deterministic per-document step, cached by sha256.
- On by default after smoke: title-block numbers, titles, revisions and dates; legend and notes regions; Legend Phase A crops; keyed notes; spec coverage p2 (4.2.2).
- Gated: `text_layer_in_subs` default off with token accounting in `plan_page` (4.2.3); OCG spike as a script; the render and effort experiment (4.2.4); core A/B with text-in-subs on vs off.

**Exit criteria.** Sheet number and title at least 95% correct on labeled sets. Legend crops cause no regression. The text-in-subs decision is recorded with data (at least +3 reading-class lines and no kind worse than noise, otherwise it stays off). Extraction p90 within budget, never blocking other runs. An adopt-or-drop decision on OCG recorded.

**Cost per run:** extraction $0; legend crops -$0.5 to +$0.3; keyed notes +$0.1 to $0.3; text-in-subs, if enabled, +$1.5 to $2.5.

**Migration M1 (dev first):** extend the `llm_jobs.job_type` CHECK if extraction becomes a queue job; the `ai_spend_ledger` view.

### 6.4 Phase 3: schedules, census, row ids (weeks 9 to 12)

- Prerequisite: answer the breakers-as-rows question from the 66 golden BOQs.
- Schedule hybrid (4.2.5), narrowed census (4.2.6), text-tag floors (4.2.7).
- Short code-assigned row ids with `likely_supersedes` (4.5.2); code applies escalation patches by id.
- Pre-emptive small-text tiles with a per-run cap (4.2.8).
- Size guards on roll-up (if re-enabled) and main: input checked against 240k and output against 128k, with a clear refusal or a planned split instead of a silent 2x bill.
- Commonly-missed constant (4.9.1), gated on smoke.

**Exit criteria.** Recall up on breakers, disconnects, equipment connections and fixtures by type. Schedule cells at least 98% correct on 10 sheets from at least 6 engineers. Census precision at least 60%. 100% row conservation. No kind worse than noise.

**Cost per run:** about -$0.5 to +$0.3. **Migrations:** none.

### 6.5 Phase 4: lengths and conductor math (weeks 13 to 16)

- Truth and baseline first: annotation reader over the 33 markup PDFs, count N, record the model-length baseline (4.3.5); estimator Bluebeam calibrations as scale labels.
- Conductor grammar (4.3.4).
- Per-viewport scale (4.3.1), precedence and conflict guard (4.3.2), `SUB_PAGE.viewports[]`.
- Structured routes, `measure_route()`, vertical legs from owner conventions, no double measurement of overall and enlarged plans, code-built markup lines, lengths recomputed after modify; side by side first, then code fills per kind (4.3.3).
- Selected-scale and viewport-diagonal guards; branch wire bands v1 (4.3.6).

**Exit criteria.** At least 95% of scaled viewports correct on 20 labeled projects. All half-size sets flagged. Zero lengths on NTS or conflicting scales. Trench and saw cut within 15% on at least 70% of the N projects, or a clear gain over baseline. Zero conductor arithmetic mismatches.

**Cost per run:** +$0.3 to $0.6. **Migrations:** none.

### 6.6 Phase 5: evidence and provenance (weeks 17 to 21; 17 to 19 if source_refs are not justified)

- Structured points with code conversion, tile-overlap dedupe, compliance metric; marked plans switch from v0 to points; position cap to about 250 if approved (4.5.4).
- View-label stamping, `plans_reader_documents.sha256`, crop route (4.5.5).
- Remaining Tier 1 checks (4.4.8); findings jsonb and the Checks list with jump-to-sheet; no weighted score (4.4.7).
- Annotated full-size PDF; humanization; optional `bid_changers` (4.5.6).
- Conditional on attribution: source_refs, `reconcile_boq` on every BOQ version and the funnel (4.5.3).

**Exit criteria.** 0 unaccounted pages. At least 0.8 to 0.9 dots per counted device on Five Star and YMCA. Under 1% unverifiable references. If source_refs are built: at least 90% of device and fixture EA lines sourced, at least 95% reconciled, `assumed_no_view` share reported.

**Cost per run:** -$0.25 to +$1.0. **Migration M3 (dev first):** `plans_reader_documents.sha256`; the outputs scope CHECK widened only if evidence and coverage leave jsonb.

### 6.7 Phase 6: addenda and the learning loop (weeks 22 to 26)

- Revision runs and the delta workbook (4.8.4); Revision tab, then the pixel overlay (4.8.5); parent shows "Superseded"; optional suggestion of a revision run on a splitter addendum.
- Bias report once at least 10 feedback pairs exist; developer-drafted, owner-approved prompt rules batched into one core sweep per quarter (4.1.6).

**Exit criteria.** At least 85% of the estimator's changed lines appear on the 16 addendum projects, with under 10% false deltas. Revision run cost at most 30% of a full run.

**Cost:** $5 to $8 per typical addendum vs about $26. **Migration M4 (dev first):** `runs.parent_run_id`, `revision_label`, `plans_reader_files.sha256`, deliverable kind CHECK plus `revision_delta`.

### 6.8 Track B: G3 price reference and correction capture (parallel from week 5)

**B1 (weeks 5 to 10, $0 per run).** Security first (7.8). Then the normalizer (7.2), cost tables and a dev-only backfill from the 10-01 backup (7.1), quality labels (7.4), the LOPO gate (7.6), the estimator estimate audit (7.7) and correction capture (4.1.5).

**B2 (built from week 11, hidden until the gates pass).** Read-only Pricing tab computed lazily per estimate version; suggested xlsx from a dedicated gated route (7.5); ROM range at intake; automatic backtest; the G3 labor adjustment as a dollar range by wage_type, after the owner clarifies what `labor_amount` includes (decision 19).

**Exit criteria.** At least 95% of the 76 workbooks ingest. The audit runs on every sealed round in dev, and an Estimating Admin judges at least 60% of flags useful on 10 historical projects. Every route passes the role tests. Feedback rows land within 5 minutes. Per-kind visibility approved by the owner.

**Migration M2 (dev first):** the cost tables and `plans_reader_feedback`. Prod only with approval, together with any post-10-01 ingest (which needs a backup refresh).

**Staffing note.** Track B assumes a parallel developer or subagent stream. With serial staffing, B1 follows Phase 3 and the timeline grows by about 5 weeks.

### 6.9 Migration discipline

- One migration owner assigns numbers at build time. Dev already holds unreleased 0154 to 0158, and several fit analyses each claimed 0163. Labels M1 to M4 in this doc are placeholders.
- Every migration is applied to dev BDR first, then staging at release time, then BDR_Prod only with explicit owner approval, one migration at a time (decision 28). Reload PostgREST after DDL.
- Tables holding cost data enable and force RLS with no policies (service role only), like 0152.

### 6.10 Timeline summary

| Weeks | Track A | Track B | Visible to estimators (in test state) |
|---|---|---|---|
| 1 to 4 | P1 measure and fixes | - | Truthful errors, marked plans with real dots, fittings never silently missing, cheaper chat |
| 5 to 8 | P2 text layer | B1 security, normalizer, library | Sharper legends, exact keyed notes, spec coverage, sheet titles on labeled sets |
| 9 to 12 | P3 schedules and census | B1 LOPO, audit, capture; B2 build starts | Breaker and disconnect recall, schedule-vs-plan contradictions; estimate audit in dev |
| 13 to 16 | P4 lengths | B2 (hidden) | Measured trench and saw cut, conductor math |
| 17 to 21 | P5 evidence | B2 gates evaluated | Checks list, annotated full-size PDF, sourced BOQ rows |
| 22 to 26 | P6 addenda and learning | Per-kind pricing visibility if gates pass | Addendum delta workbook at $5 to $8 |

## 7. Suggested pricing design

The goal is a G3 price reference that helps estimators and G3 reviewers, never a price. Everything in this section is deterministic Python, $0 per run, internal only.

### 7.1 Ingestion from estimator workbooks

- **Source.** 76 estimate workbooks across 68 projects in the 2026-10-01 backup. 73 share one 15-column template. After dropping superseded revisions and about 1,000 LS allowance rows, about 3,422 priced lines (2,067 distinct descriptions) remain. Units: EA 2,806, FT 1,154, LS 1,002, plus CU FT, SQ FT and strays. Coverage of H (labor) and I (material) is 100%/100% for conductors, conduit, breakers, disconnects, receptacles and grounding; fixtures 100%/99% (material values are placeholders that vendor quotes later replace); trench 100%/77%; demolition labor only.
- **Parser.** A pure openpyxl parser in `app/services/estimate_parse.py`, shared by the eval scorer (4.1.2) and the ingester. It reuses header detection and the section walk from the golden scorer, reads literal H and I values and the labor rate in N4 ($108 in every file), never calls a model, and is cached by file sha256 plus `PARSER_VERSION`, the pattern `auto_send_estimate.py:48` already uses for `parse_recap`.
- **Tables (M2, dev first).**
  - `cost_sources`: one row per parsed file: project_file_id, project_id, sha256, parser_version, workbook date, submission round, `superseded`, status, line count, errors. Unique on (project_file_id, parser_version).
  - `cost_observations`: one row per priced line: source, project, sheet, row number, raw and normalized description, BOQ group and subgroup, quantity, wastage, uom, unit labor hours, unit material cost, labor rate, observed_on, snapshot features (wage_type, labor_needed, project_type), matched item, match method and score, `excluded` with a reason (superseded, allowance, outlier, zero-material demo).
  - `cost_items`: canonical items: item_key (for example `conduit|emt|3/4`, `wire|thhn|cu|12`, `breaker|20a|1p`, `recep|duplex|gfci`), family, attrs jsonb, G3-style canonical description, base uom, aliases, and a pg_trgm-indexed `search_text`.
  - Benchmarks (median, p25, p75, n projects, recency) are computed on the fly from observations. No `cost_benchmarks` table until performance needs one.
- **Ingest triggers.** A dev-only backfill script over the local backup (decision 23). Later, a non-LLM job on each sealed estimator round, sharing the `on_estimator_round` hook with correction capture. Prod ingest only with approval and a fresh backup.

### 7.2 Normalization and matching

- Promote `normalize_description`, `_SYNONYMS`, `key_tokens` and the conflict penalties from `scripts/plans_reader_golden.py` into `app/services/electrical_items.py`, guarded by a golden-score regression test so the scorer's numbers do not move.
- **Family gate first:** the AI row's `material_category` plus the estimator group and subgroup decide the family (conduit, wire, breaker, receptacle, switch, fixture, gear, LV, trench).
- **Hard rejects on attributes:** any conflict on size, amps, poles, material (CU vs AL), conduit type (EMT vs PVC vs RGS) or fixture family rejects the candidate. A high text score never overcomes it. A wrong-size match is worse than no match.
- **Then rapidfuzz** within the family for ranking. Ladder: exact item_key, similar (same family, no conflict, high similarity), context (same family only), none. Top 3 candidates returned with their source projects.
- **Fixture tags** (A, F2) are project-local and excluded from cross-project identity; fixtures match on family plus catalog prefix (413 of 557 fixture lines use "TAG, MANUFACTURER, CATALOG").
- **UoM handling is case-sensitive and explicit:** FT = LF; C = 100 FT and M = 1000 FT (lower-case "m" is never read as 1000); SQ FT = SF; CU FT = CF and CY = 27 CF; EA = PC; LS never converts; LOT, ROLL and BOX return "source-backed conversion needed". The conversion fact string is stored with every suggestion.
- **No embeddings now.** The house style is heavily abbreviated and matches better on attributes than semantics. pgvector is a later option for spec text.

### 7.3 Benchmark statistics

- Labor units: median and IQR across projects, not recency-decayed. The top items cluster tightly (3/4"C EMT 0.032 hr/FT, CV 0.13, 83 rows; #12 THHN 0.010 hr/FT, CV 0.11; GFCI duplex 0.51 hr, CV 0.17).
- Material: median and IQR with a recency weight (proposed half-life 180 days). Copper conductors are flagged commodity-volatile; recency weighting for copper is decision 20.
- The estimator is internally consistent: per-project labor index IQR 0.93 to 1.03, and normalizing by the project index cuts the median absolute deviation from 10.8% to 8.3%. The LOPO gate reports both raw and per-project-normalized error.

### 7.4 Confidence labels and the G3 adjustment

- **Labels (statistical, not lexical):**
  - `strong`: exact key, at least 5 projects, IQR/median at most 0.25, seen within 12 months.
  - `good`: exact key with 2 to 4 projects or spread 0.25 to 0.5; or a similar match with at least 5 projects.
  - `weak`: one project, a context match, or spread over 0.5. **Fixture and gear material are capped at weak**, because the estimator's values are placeholders until vendor quotes arrive.
  - `missing`: no credible candidate; the cell stays blank.
  - Thresholds are starting points tuned by LOPO.
- **G3 labor adjustment factor.** G3's final `labor_reviews.labor_amount` is a median 1.43x the estimator's labor cost (p25 1.19, p75 2.12, n=47). Both sides are dollars at the fixed $108 rate, so the factor is shown only as a **dollar range by wage_type** (62 PW projects, 14 non-PW), with n and IQR, falling back to the global segment when n is under 8. It is never presented as hours, never written into a unit cell, and shown only at Verify or after labor entry. Before it ships, the owner clarifies what `labor_amount` includes (supervision, PW fringe, contingency) (decision 19). Recommend G3 start filling the unused `labor_reviews.labor_breakdown` with per-section reasons, so the uplift can later be explained per section.

### 7.5 How it shows in the estimate xlsx and the UI

- **The existing estimate deliverable stays price-free.** `_NO_PRICING` stays in every sub, roll-up, escalation, main and modify prompt, and no pricing kind enters `DELIVERABLE_KINDS` (`plans_reader_schemas.py:443`) or the `CHAT_TOOLS` enums (`:450`).
- **Suggested xlsx** from a dedicated gated route (`GET /ai-estimating/pricing/runs/{id}/suggested.xlsx`), built on demand from the current BOQ version, never stored as a deliverable and never copied into `project_files`. Columns H and I stay blank so nothing reads as the estimator's number. Suggestions go in side columns S to X: SUGG LABOR HR, SUGG MATERIAL, QUALITY, N PROJECTS, RANGE (p25 to p75), MATCHED ITEM, with a cell comment for provenance ("median of 9 G3 projects, 2026-03 to 2026-09"). Missing rows get a light yellow fill. An INTERNAL watermark in the header. Because our writer emits no J to O, subtotal or recap formulas today, the side-column variant needs none; if the owner later chooses H and I (decision 15), the full template formulas (`J = H*N$4`, `K = H*F`, `L = I*F`, `M = J*F`, `N = I+J`, `O = N*F`, P/Q subtotals, recap links) must be written, with tax and OH&P left for the estimator.
- **Pricing tab** in RunView (added to `RUN_TABS` only for pricing viewers): read-only grid by BOQ section with quality chip, range and n; rows expand to candidates with project numbers and dates; header shows estimator-basis labor hours and material and the quality share ("71% strong, 14% good, 6% weak, 9% missing"). Computed lazily per estimate version; no accept/override workflow in v1, since estimators price in their own workbook.
- **ROM at intake:** AI quantities times benchmark medians, shown as a range with the recall caveat, only for kinds that passed the gates.
- **Recall caveat everywhere:** a precise-looking total at about 30% recall misleads. Every pricing surface states the takeoff recall for the kinds it covers.

### 7.6 The LOPO gate

- A 300-pair gold set across 25 non-holdout projects, each line labeled with its canonical item key by an Estimating Admin (about 3 hours, decision 17; commodity rows near-automatic).
- Leave one project out: build benchmarks without project P, suggest for P's lines, compare. Metrics per kind: top-1 and top-3 key accuracy, "no credible match" rate, **false-confident rate** (strong or good label, wrong key: the dangerous case), unit labor and material error (raw and normalized), coverage (share of lines strong or good), project labor-hours total within 10%.
- An AI-wording track is added once Phase 3 eval runs exist: AI BOQ descriptions are noisier than estimator descriptions.
- Per-kind thresholds are owner decisions (16). Proposed defaults: false-confident rate under 2%, median unit error under 15% for strong, coverage over 60%, and core-tier takeoff recall for the kind at least 60%.

### 7.7 Estimator estimate audit (the earliest pricing value)

- On every sealed estimator round (same hook), parse the estimate and flag unit labor or material outside p25 to p75 plus a margin for strong and good items, and quantity deltas against the AI BOQ when a linked run exists.
- Shown at Labor and Verify for pricing viewers. Optionally holds auto send (0159) until reviewed (decision 18).
- It does not depend on AI recall: it audits the human estimate against G3's own history. This is why it ships in B1, not B2.

### 7.8 Security: internal only, never the estimator role

- New dependency `require_pricing_view`: Executive, IT Admin and Estimating Admin by default (decision 14). It excludes the external estimator always, and the Accountant unless the owner says otherwise. `require_internal` admits the Accountant (`core/roles.py:36`), so pricing cannot reuse it.
- Routes under `/ai-estimating/pricing/*`, behind `require_plans_reader`, the AI_ESTIMATING_STATE gate, `AI_PRICING_ENABLED` (default off, exposed through `/features`) and `require_pricing_view`.
- New tables enable and force RLS with no policies.
- Tests: no pricing kind in `DELIVERABLE_KINDS` or `CHAT_TOOLS`; 403 for the estimator and the Accountant on every pricing route; the generic deliverable download and preview routes return 404 for anything pricing; no estimator-portal listing returns plans_reader files.
- `file_access.record` (0161) logs pricing downloads under their own source.
- Whether cost data may ever reach a model provider through chat is decision 21. Default: no; pricing stays deterministic and chat has no pricing tool.
- Eval outputs carrying dollars stay local and out of the repo.

## 8. Evaluation plan and the learning loop

### 8.1 Golden set

| Tier | Size | Use | Cost per sweep (standard) |
|---|---|---|---|
| smoke | 4 (YMCA, Five Star, one small TI, one short roadway) | every prompt, schema, packing or render change | about $50 |
| core | 12 (4 TI, 3 roadway/site, 2 new construction, 1 casino, 2 spec-heavy public) | phase exits, A/Bs | about $180 to $250 |
| holdout | 15, fixed now | final go/no-go only; never feeds mining, rules or thresholds | about $250 |
| full | 66 | optional baseline | $1,000 to $1,400 |

66 golden projects have drawings, estimator BOQ and estimate; 65 have marked plans, 43 have specs, 33 have trench or saw cut markups, 16 have addenda. A fresh prod backup (with approval) adds post-10-01 projects as unseen holdout.

### 8.2 Metrics

- Per kind: exact rate, within 1, aggregate per key-token bucket, signed bias; lengths banded; gear rating identity; wiring-method agreement reported separately (a spec-reading failure, not a counting one).
- Overall: exact recall, precision, dollar-weighted error (local only).
- Pipeline: cost per run, cost per exactly matched row, sub groups lost, unverifiable reference rate, dots per device, coverage states.
- Pricing (Track B): the LOPO metrics of 7.6, and later suggested totals vs the estimator Bid Recap and vs `proposal_sends`.

### 8.3 Regression tracking

- A run's `settings_snapshot` carries a `prompt_hash` (sub, category focus, roll-up, escalation, main, schema JSON) and the code version, so every run is traceable to its prompt.
- Ship rule (documented, not CI-enforced, because CI cannot spend money): a smoke sweep for any prompt, schema, packing or render change; a core sweep for CATEGORY_FOCUS or MAIN_SYSTEM changes. A regression is a drop beyond the measured noise band at the aggregate level.
- CI at $0: the replay test re-scores stored v1 specs; every pure-function check has a backtest over stored outputs.
- Runner reports (JSON plus a short markdown summary in the scratchpad, never committed with dollar figures) until 5 or more sweeps justify an Accuracy tab.

### 8.4 Estimator style is not truth

The reference is "agreement with the external estimator" (decision 3). Estimators miss items too, and G3 adjusts their labor a median 1.43x. Mitigations: the aggregate metric (granularity-robust), the four miss classes, about 1 hour of Estimating Admin adjudication of the top 10 dollar misses per sweep, and an `estimator_miss` outcome in feedback.

### 8.5 The learning loop

1. **Capture** (Track B1): every sealed round diffs against the linked AI run into `plans_reader_feedback`, with `input_changed` flagged when the round's drawings differ.
2. **Adjudicate:** Estimating Admin labels a sample `ai_false_positive`, `estimator_miss` or `ambiguous`. `boq_training_examples` seeds the taxonomy.
3. **Aggregate** (P6): per-kind bias once there are at least 10 pairs.
4. **Propose:** the developer drafts prompt rules or commonly-missed entries from the bias report. Holdout projects never contribute.
5. **Approve:** the owner (or a named approver, decision 26) approves.
6. **Verify:** one core sweep per quarter with the batch; rules stay only if target kinds improve and no kind is worse than noise.
7. **Retire** rules whose kind stops showing bias.

Lessons never carry prices. Pricing calibration is the G3 adjustment layer of 7.4.

### 8.6 Data plan

| Asset | Use | Phase |
|---|---|---|
| 66 golden sets | manifest tiers, scoring, attribution; non-holdout only for mining | P1 onward |
| 76 workbooks, 3,422 priced lines | estimate-line parser: dollar weights, cost history, branch bands, commonly-missed, audit | P1, P3, P4, B1 |
| labor_reviews (n=47, 1.43x median) | G3 adjustment as a dollar range by wage_type | B2 |
| 43 spec sets | spec coverage; optional bid_changers | P2, P5 |
| 33 trench and saw cut markup PDFs | length truth, Bluebeam scale labels | P4 |
| 65 marked plans (flattened) | qualitative dot comparison only | P1, P5 |
| 16 addendum projects, 9 round-2 pairs | addenda eval | P6 |
| Multi-round estimator BOQs | estimator noise ceiling | P1 |
| bid_split_training_examples | splitter and routing accuracy | P1 |
| plans_reader_calls of golden runs | per-kind cost split | P1 |
| OCG layers (about 20% of files), SHX (about 12% of pages) | spike; text source | P2 |
| Estimator NOTE rows, 48 proposal_drafts scope lines | commonly-missed seed (owner permission) | P3 |
| proposal_sends | pricing backtest: estimator total vs final G3 bid | B2 |
| boq_training_examples | adjudication taxonomy seed | P6 |

## 9. Model and cost plan

### 9.1 Per step

| Step | Model now | Est. cost per 25-sheet run | Alternatives (later, owner-gated) |
|---|---|---|---|
| Sub reading (vision) | gpt-6-astra, medium | about $15 to $20 (dominant) | Flex about 50% off; Claude only after a golden A/B and vendor approval |
| Escalation | gpt-6-astra | within the 0.5x budget | same |
| Roll-up | off (code) | $0 (was $1 to $4) | re-enable per setting |
| Main (text) | gpt-6-astra | about $3 to $6 | a flat-priced 1M-context model if a main exceeds 272k |
| Splitter roles | claude-opus-5 / gpt-5.4-mini (existing) | needs verified price rows | n/a |
| Text-in-subs (if enabled) | gpt-6-astra | +$1.5 to $2.5 | n/a |
| Page facts, schedule validation, census, scale, conductor math, guards, integrity, coverage, cost library, audit, LOPO | deterministic | $0 | n/a |
| Future text reviewer | gpt-6-astra | $1.5 to $3 | second vendor if approved |

### 9.2 Run cost trajectory (about $26 today)

| After | Change |
|---|---|
| P1 | -$1 to -$4 on multi-group sets |
| P2 | $0 to +$2.5, depending on text-in-subs |
| P3 | -$0.5 to +$0.3 |
| P4 | +$0.3 to +$0.6 |
| P5 | -$0.25 to +$1 |
| Track B | $0 |

Net: about $22 to $30 at standard tier, about $11 to $15 on Flex. Addenda $5 to $8 per revision. Optional shadow mode (auto-start a run when a project reaches the estimator) $15 to $26 per bid on its own sub-budget (decision 24).

### 9.3 Eval budget (dev only, separate from the prod cap)

| Item | Cost |
|---|---|
| P1 noise floor, smoke and core (includes the Five Star roll-up check) | $350 to $450 |
| Phase-exit core sweeps (P2 to P6, 5 exits) | about $200 each, about $1,000 |
| P2 text-in-subs A/B and render experiment | about $250 to $300 |
| Optional full-66 baseline | $1,000 to $1,400 |
| **Total without full-66** | **about $1,600 to $1,750 standard, about half on Flex** |

LOPO and all Track B evals are $0.

### 9.4 Spend cap changes

- No purpose split in `check_cap` for now. Evals run on dev; the dev cap is raised per sweep with the existing `set_cap`; the runner enforces the owner-approved sweep budget client-side. A separate sub-budget is needed only for prod shadow mode.
- Splitter spend joins the AI Estimating cap through the `ai_spend_ledger` view (P2).
- Per-run ceiling at 1.25x `cost_usd_max` (P1).
- Cache-write multipliers and verified price rows before any Anthropic-priced call counts.
- The prod daily cap stays $75 unless the owner changes it in Settings.

### 9.5 Provider prices to verify before relying on them

The Claude prices quoted in the fit analyses ($4/$20 and $2/$10 per M, cache read $0.20), the 32 MB request cap and the image-count caps are unverified here. gpt-6-astra Flex support and its retention requirement are unverified. Each is checked against the live pricing page, with a check date, before any `llm_pricing` row is added (the module already requires this).

## 10. Risks and open questions

### 10.1 Risks

1. **Estimator style is not truth.** Mitigations: aggregate metric, four miss classes, top-10 dollar adjudication per sweep, `estimator_miss` outcome.
2. **Small n and nondeterminism.** Measured noise band, aggregate-level rules, holdout confirmation; no per-kind 2-sigma rules.
3. **Recall may barely move** if misses are mostly convention. P1 classification resets targets before P2 and P3 claims.
4. **Token growth toward 240k and 272k.** Exact accounting in `plan_page`, caps, title-block dedupe, P3 size guards.
5. **Prompt injection through the text layer.** textclean, hidden text dropped, data framing.
6. **TI breaker overcount in the census.** Answer from the golden BOQs and gate breaker counting.
7. **Schema ripple** across about 470 tests and the modify path. Tolerant reads, nullable strict fields, re-keying after modify.
8. **Pricing leakage** through `DELIVERABLE_KINDS`, chat or the Accountant role. A separate gate and route, enforced by tests.
9. **Precise-looking totals at about 30% recall.** Side columns, recall caveats, double gating on LOPO and recall.
10. **Eval spend vs the cap, and the Mac sleeping** during long local runs. Dev cap raises, runner budget, `caffeinate`, `--run-id` attach, token refresh.
11. **Parallel tracks collide** on migration numbers and uncommitted trees. One migration owner; never a blind checkout of tracked files.
12. **Little live signal while prod is in test state.** Shadow mode on a sub-budget, or a move to live with explicit criteria.

### 10.2 Decisions for the owner (each with a recommended default)

**Before week 1**
1. Eval budget about $1,600 to $1,750 (half on Flex) and per-sweep dev cap raises. *Default: approve.*
2. Flex as an eval and economy option, once gpt-6-astra support and retention are confirmed. *Default: approve for evals; New Run option after one smoke sweep.*
3. "Agreement with the external estimator" as the scoring reference, with about 1 hour of Estimating Admin adjudication per sweep. *Default: accept.*
4. Roll-up off by default. *Default: approve after the Five Star rerun.*
5. House rules: auto-correct unambiguous cases with a visible record, or flag only. *Default: auto-correct the listed cases.*
6. Core and holdout split; the holdout never feeds lessons, mining or thresholds. *Default: approve.*

**Phases 2 to 5**
7. Enable text-in-subs if the A/B passes (+$1.5 to $2.5 per run). *Default: enable only at +3 reading-class lines.*
8. Breakers as BOQ rows, if the golden BOQs leave it ambiguous. *Default: new and modified panels only.*
9. Measurement conventions: makeup per termination, mounting and ceiling heights, stub-up per end, MC vs EMT default for TI. *Default: makeup 0, verticals from spec or notes, flag when unknown, follow the spec on wiring method.*
10. Code lengths side by side vs replacing the model's value. *Default: side by side; owner signs each per-kind switch.*
11. Position cap 60 to about 250; marked plans as letter pages, annotated full-size PDF, or both. *Default: raise the cap; produce both.*
12. Spec divisions to prove. *Default: 26/27/28 plus 31 and 33 headers when present.*
13. When code and model disagree on a quantity, which goes in the BOQ. *Default: the model value with a flag until the kind passes eval, then the code value with the model value kept.*

**Track B**
14. Pricing roles. *Default: Executive, IT Admin, Estimating Admin; not Engineer, not Accountant.*
15. Side columns vs H and I. *Default: side columns S to X.*
16. Per-kind LOPO and takeoff-recall thresholds before suggestions are visible. *Default: section 7.6 values.*
17. About 3 hours to label 300 pairs. *Default: approve.*
18. Whether the audit can hold auto send (0159), and where its flags appear. *Default: flags at Labor and Verify; no hold for the first month, then decide on data.*
19. What `labor_amount` includes, and where the adjustment shows. *Default: shown only at Verify as a dollar range, after clarification.*
20. Copper recency weighting. *Default: 90-day half-life for copper, 180 for other material.*
21. Whether cost data may ever reach a model provider through chat. *Default: no.*
22. Ask the estimator where their labor units come from (NECA MLU, Accubid). *Default: ask; it affects how we label provenance.*
23. Approve the dev backfill from the 10-01 backup. *Default: approve.*
24. Shadow mode and its sub-budget. *Default: off; revisit after P3.*

**Phase 6 and release**
25. Delta workbook vs a delta tab; whether to suggest a revision run on a splitter addendum. *Default: separate workbook; suggest, never auto-start.*
26. Who approves prompt rules. *Default: the owner, with Estimating Admin input.*
27. Criteria for moving `AI_ESTIMATING_STATE` from test to live. *Default: section 6.1.*
28. Each prod migration (M1 to M4) separately. *Default: one at a time at release, with a fresh backup.*
29. A fresh prod backup. *Default: before M2 reaches prod.*
30. Whether a second AI vendor (and which provider tiers, given retention terms) is ever allowed for bid sets. *Default: not yet; revisit after Phase 3 with an A/B plan.*

### 10.3 Claims still to verify

- gpt-6-astra Flex support and retention terms.
- All Claude prices, the 32 MB and image-count caps.
- Text-layer share: 77% (229 pages, 60 PDFs) vs 82% (107-page sample); re-measure on core in P2.
- "Output was $8.69 of $25.99" on Five Star: recompute from `plans_reader_calls`.
- The 1.43x ratio is dollars over dollars at a fixed $108 rate; never present it as hours.
- N, the number of the 33 markup PDFs that can be matched and scaled: unknown until P4 week 1.
- Text-tag floor precision and any reviewer precision: unmeasured.
- The 470-test figure and the next free migration number: confirm at build time.

## Appendix A. Bidwright files studied (paths only, no code)

All paths are relative to the Bidwright repository root. Read as data; nothing was executed.

**Evidence, integrity, provenance**
- `packages/mcp-server/src/tools/quote-tools.ts` (row write integrity gate, payload contract)
- `packages/mcp-server/src/tools/drawing-evidence-tools.ts` (claims, contradictions, cited views)
- `apps/api/src/routes/vision-routes.ts` (recorded views, detection to takeoff funnel)
- `apps/api/src/prisma-store.ts` (finalize claim check, link-driven quantities, calibration snapshots, staleness)
- `packages/db/prisma/schema.prisma` (EvidenceView, Pickup, PickupLink, calibration feedback)
- `packages/domain/src/line-derivation.ts`, `packages/domain/src/estimate-evidence.ts`, `packages/domain/src/provenance-labels.ts`
- `packages/domain/src/component-census.ts`, `packages/domain/src/document-coverage.ts`, `apps/api/src/services/document-coverage.ts`
- `packages/domain/src/estimate-validation.ts`

**Drawing reading and measurement**
- `packages/vision/python/tools/page_reader.py`, `pdf_takeoff.py`, `count_symbols.py`, `count_library.py`, `analyze_geometry.py`
- `apps/api/src/services/symbol-legend-service.ts`, `titleblock-scale-service.ts`, `pdf-takeoff-geometry.ts`, `takeoff-calibration-state.ts`, `worksheet-takeoff.ts`, `dwg-processing-service.ts`
- `apps/api/src/routes/pdf-takeoff-routes.ts`
- `packages/domain/src/measurement-format.ts`, `packages/domain/src/cad-takeoff.ts`
- `packages/ingestion/src/azure-di.ts`, `packages/ingestion/src/project-ingestion.ts`

**Pricing and labor**
- `packages/domain/src/cost-intelligence.ts`, `labor-basis.ts`, `cost-matcher.ts`, `quote-engine.ts`, `assembly-expansion.ts`
- `packages/mcp-server/src/tools/resource-tools.ts`
- `apps/api/src/services/labor-unit-matching.ts`, `apps/api/src/services/vendor-evidence-lines.ts`

**Review, evals, calibration**
- `apps/api/src/services/automatic-estimate-review.ts`, `apps/api/src/services/review-launch.ts`
- `apps/api/src/routes/review-routes.ts`
- `packages/domain/src/estimate-review-loop.ts`, `packages/domain/src/estimate-risk.ts`
- `packages/mcp-server/src/tools/review-tools.ts`, `packages/mcp-server/src/tools/system-tools.ts`
- `scripts/eval/takeoff-reference.ts`, `scripts/eval/blind-copy.ts`, `scripts/eval/run-model-matrix.ts`, `scripts/eval/outcome-score.ts`
- `scripts/agent-evals/labor-tree-navigation-harness.ts`

**Runtime, knowledge, tools**
- `apps/api/src/services/cli-runtime.ts`, `apps/api/src/routes/cli-routes.ts`
- `packages/agent/src/llm/adapters/` (anthropic, openai, gemini, openrouter, lmstudio)
- `packages/domain/src/estimate-strategy-stages.ts`, `packages/domain/src/estimate-run-plan.ts`, `packages/domain/src/revision-compare.ts`
- `apps/api/src/services/revision-diff-service.ts`, `apps/api/src/services/file-ingest/bluebeam-markups.ts`
- `apps/api/src/services/estimator-persona-prompt.ts`, `apps/api/src/services/claude-md-generator.ts`, `packages/db/src/seed-data.ts`
- `apps/api/src/services/knowledge-service.ts`, `estimator-search.ts`, `reference-book-pages.ts`
- `packages/mcp-server/src/tools/knowledge-tools.ts`, `packages/mcp-server/src/tools/response-compaction.ts`
- `packages/mcp-server/src/tool-access.ts`, `packages/mcp-server/src/agent-tool-registry.ts`, `packages/mcp-server/src/tool-discovery.ts`
- `apps/model-editor`, `apps/cad-editor` (surveyed for size only)

## Appendix B. Our files that change

Line numbers are as of 2026-10-21 and will drift.

**Backend, existing**

| File | Change | Phase |
|---|---|---|
| `app/services/plans_reader_runs.py` | `_category_output` (1770) and `_merge_outputs` (1753) code merge; sub retry beside `_advance_consolidating` (2064); `_plan_reading` (1324) probe and page facts step; `_finish_with_main` (2192) checks, coverage, reconcile; size guard in `_write_outputs_and_queue_main` (2018); `deliverable_spec` (2536) staleness and code re-render; revision runs | P1 to P6 |
| `app/services/plans_reader_agents.py` | page labels with sheet geometry; integrity gate after `_finalize` (1468); ledger legend crops in `build_ledger` (635); text parts in `_build_sub` (1503); keyed notes and schedules blocks; commonly-missed constant; `prompt_tokens` (1454) accounting; patch-mode main | P1 to P6 |
| `app/services/plans_reader_schemas.py` | `SCHEDULE_ITEM` (263) source and parse_confidence; `RUN_ITEM` (222) route; `COUNT_ITEM` (207) points; `SUB_PAGE.viewports`; `BOQ_ITEM` (310) source_refs (conditional); `addendum_changes`; optional `bid_changers` | P3 to P6 |
| `app/services/plans_reader_render.py` | marked plans and markups from parsed positions; `boq_quantity` (133) and conduit normalization guards; measured FT labels in `render_markup_pdf` (1591); annotated full-size marked plans; delta workbook | P1, P4 to P6 |
| `app/services/plans_reader_pack.py` | `plan_page` (120) output sizing by page_role, text tokens, pre-emptive tiles; `estimate_run` (469) scanned pricing | P1 to P3 |
| `app/services/plans_reader_images.py` | region crops for legends and small text | P2, P3 |
| `app/services/plans_reader_chat.py` | compaction filters and lower caps | P1 |
| `app/services/llm_pricing.py`, `app/services/llm.py` | verified price rows, cache-write multipliers, `cache_write_tokens` | P1 |
| `app/services/ai_spend.py` | per-run ceiling; ledger view as the spend source (73) | P1, P2 |
| `app/services/auto_send_hooks.py` | `on_estimator_round` (29) adds feedback capture, cost ingest and the audit | B1 |
| `app/core/features.py`, `app/core/config.py` | `AI_PRICING_ENABLED`, new plans_reader settings | P1 to B1 |
| `app/routers/plans_reader.py` | link-project, crop route, Checks payload, pricing routes behind the new gate | P5, B1 |
| `scripts/plans_reader_golden.py`, `scripts/plans_reader_live_run.py` | matcher moves to `app/services`; runner tiers, blind guard, budget | P1 |

**Backend, new**

`app/services/plans_reader_score.py`, `estimate_parse.py`, `plans_reader_checks.py`, `plans_reader_pagefacts.py`, `plans_reader_schedules.py`, `plans_reader_census.py`, `plans_reader_geometry.py`, `plans_reader_annotations.py`, `plans_reader_evidence.py`, `plans_reader_revision.py`, `electrical_items.py`, `cost_history.py`, `price_suggest.py`, `plans_reader_feedback.py`, `app/core` dependency `require_pricing_view`, `scripts/plans_reader_eval.py`, `scripts/plans_reader_pricing_eval.py`, and the golden manifest JSON beside the backup. Tests beside each.

**Migrations (dev BDR first; numbers assigned at build time)**

| Label | Contents | Phase |
|---|---|---|
| M1 | `llm_jobs.job_type` CHECK (if extraction becomes a job); `ai_spend_ledger` view | P2 |
| M2 | `cost_sources`, `cost_observations`, `cost_items`, `plans_reader_feedback` (forced RLS, no policies) | B1 |
| M3 | `plans_reader_documents.sha256`; outputs scope CHECK only if needed | P5 |
| M4 | `plans_reader_runs.parent_run_id`, `revision_label`; `plans_reader_files.sha256`; deliverable kind CHECK plus `revision_delta` | P6 |

**Frontend**

`bdr_fe/components/aiEstimating/RunView.tsx` (`RUN_TABS`, line 86: checks, pricing for pricing viewers, revision), new `ChecksPanel`, `PricingPanel`, `RevisionPanel`, DocumentTree extraction badges, NewRunPanel economy and revision options, `bdr_fe/lib/aiEstimating.ts` types, `bdr_fe/lib/features.ts` pricing flag, and strings in all six locales (about 0.5 day per new panel).

**Docs**

`docs/PLANS_READER.md` gains a build-log section per phase and the ship rule of 8.3; this roadmap is updated at each phase exit with measured numbers.
