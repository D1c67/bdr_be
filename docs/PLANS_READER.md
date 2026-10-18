# AI Plans Reader (design draft)

Status: DESIGN DRAFT, 2026-10-17. Nothing is built. No migration, no code, no flag. This doc records the measured facts, the proposed architecture and the decisions still open before a build starts. Companion: the token calibration page (private artifact, see the session notes) and `docs/BID_INTELLIGENCE.md` (2026-09-23 research on what to extract).

Owner's brief (2026-10-17): a standalone page, not connected to the bidding pipeline for now. GPT-6 Astra at medium reasoning reads the plans and documents. Files that are not labeled, plus general drawings, go through the Bid File Splitter. One set of calls per splitter output category (`sub_plans_agents`), split further into groups when a category is too big (`tiered_plans_agents`), the ledger sent with every group, a `main_plans_agent` that consolidates everything including specs, all outputs stored with the document type and file they came from. The main agent produces the deep brief plus everything the app needs to build the BOQ, the markups and the estimate files for the electrical scope only. Users chat with it, reference any file or output, toggle "Pro Max" to let it look at the actual files, and ask it to modify a BOQ, markup or estimate. Sub agents also return notes and metadata tags per page and detail. Pages go over as PNG at 260 ppi, the model says when resolution is too low and the app re-sends higher. No pricing anywhere.

## 1. Measured facts that shape the design

Model: `gpt-6-astra` (OpenAI, released 2026-09-03). The project's `OPENAI_API_KEY` can call it. Verified against the OpenAI model page and the images-vision guide on 2026-10-17, and against 22 live calls on real G3 sheets from the 2026-10-01 production backup.

| Fact | Value |
|---|---|
| Context window / max input / max output | 1,050,000 / 922,000 / 128,000 tokens |
| Pricing per 1M tokens | $10 input, $1 cached input, $12.50 cache write, $50 output. Requests over 272,000 input tokens bill the whole request at 2x input and 1.5x output. Batch and Flex are 50% off. |
| `reasoning.effort` | low, medium, high, xhigh, max (no none) |
| Image billing | 32 px patches, `ceil(patches x 1.2)` tokens. `detail: high` shrinks to a 2,500-patch budget (about 3,000 tokens). `detail: original` sends native pixels and REJECTS any image over 30,000 patches. |
| Image request limits | 512 MB payload, 1,500 images per request. The 50 MB cap is for `input_file` PDFs (per file and combined per request). |
| Rate limit on this key | 1,000,000 tokens per minute, 5,000 requests per minute |
| Spec PDF via `input_file` | 1,036 input tokens per page measured on an 18-page text section (OpenAI sends text plus a page image per page) |

What one plan sheet costs (input tokens, measured):

| Sheet | 260 ppi PNG, detail high | 150 ppi PNG, detail original | 260 ppi as 3x2 native tiles, one call |
|---|---|---|---|
| 42x30 (most common G3 size) | 3,003 | 33,446 | 112,685 |
| 36x24 | 2,910 | 23,030 | about 68,600 (computed) |
| 48x36 | 2,971 | rejected at 150 ppi (38,194 patches); 133 ppi max | about 137,500 (computed) |

A full 42x30 sheet at 260 ppi is 10,920 x 7,800 px = 83,448 patches. It cannot be sent natively. At `detail: high` OpenAI shrinks it to about 1,830 x 1,310 px, which is 44 ppi: titles and large notes only. The 260 ppi detail the brief asks for only survives as tiles, at 3.4x the tokens of one native 150 ppi image. On the Edikted E-101 power plan the single 150 ppi native image was enough for the model to read the scale, the keyed notes and the receptacle symbols (output 3,224 tokens, 59 s).

Output per sheet on a real count prompt at medium effort: 766 to 3,224 tokens, of which 52 to 2,054 were reasoning.

Set sizes from the 49 submitted or decided projects in the backup: electrical drawings median 16 pages (mean 55, max 1,030), other drawings median 20 pages (max 462), specifications median 239 pages (max 1,185) in 33 of the 49 projects. Sheet sizes: 42x30 (1,757 pages), 36x24 (1,057), 48x36 (362), letter and tabloid (1,700).

Cost per median project (16 electrical sheets, 20 other sheets, 239 spec pages, sub agents plus main agent, input plus output): about $17 with the native strategy, about $52 with 260 ppi tiles on every sheet. Largest set in the backup (CSN Collegewide Access Controls, 1,030 electrical pages): $345 native input only, $1,160 tiled.

## 2. Resolution strategy (recommendation)

Default per drawing sheet: one PNG at the largest ppi that stays under 30,000 patches for that sheet size (155 ppi for 42x30, 188 for 36x24, 133 for 48x36, 260 for letter and tabloid), sent with `detail: original`. The sub agent returns `resolution_flags: [{page, region, reason}]` naming regions it could not read (regions as a 3x3 grid cell or a bounding box in page fractions). The app re-renders only those regions at 260 ppi (or 2x the first pass, whichever is higher), tiles them to stay under 30,000 patches each, and re-asks the same agent for that sheet with the tiles plus the ledger. The escalation call carries the first-pass JSON for that sheet so the model patches rather than restarts.

Alternative the owner may prefer: every sheet at 260 ppi, always tiled (3x2 on 42x30, 2x2 on 36x24, 3x3 on 48x36, 5% overlap, all tiles of a sheet in one call so the model sees the whole sheet). Cost 3.4x. Both strategies are a per-run setting (`render_mode = adaptive | tiles_260`) so they can be compared on the golden set before one becomes the default.

PNG, not JPEG, for line art. Billing is by pixels, not bytes; JPEG q85 was often larger than optimized PNG on these sheets.

## 3. Pipeline

### 3.1 Run creation
A run starts from the `/plans-reader` page with either uploaded files (PDF, xlsx, docx; 450 MB per file like the rest of the app) or the files of an existing project chosen from a picker (server-side `storage.copy_file`, read only; the project is never modified). Each file can be pre-labeled by the user with a splitter category (electrical drawings, mechanical drawings, specifications, RFP, addenda, other, ...). Unlabeled files and files labeled general drawings go to the splitter. The page shows an estimated cost before the run starts, computed from page sizes with the patch formula, and refuses to start above `PLANS_READER_MAX_INPUT_TOKENS_PER_RUN` (default 10M tokens, about $100) unless an executive or IT admin overrides.

### 3.2 Split (reuse the Bid File Splitter)
A `bid_split_jobs` row with `source='plans_reader'` and no `project_id`, so none of the project re-filing side effects fire (`rfp_split.resync_after_run`, `file_project_job_whole_quietly`). Rows are staged with `bid_split.stage_upload` semantics from server-held bytes. Pre-labeled files skip the splitter and become one document each.

Splitter change (built 2026-10-17): `page_role` in `_PAGE_SCHEMA` with the enum `cover_index | legend_symbols | general_notes | schedule | plan | detail | diagram | site | spec_text | other`, persisted per segment on `bid_split_segments.page_meta jsonb`. Shape as built: entries are in SEGMENT-PDF order, each `{pdf_page (1-based page inside the segment object), page (source page), title, page_role, confidence, prepended: bool}`; `prepended: true` marks the General / Cover Sheets copied to the front of a trade segment, and the ledger must skip those so they are not counted twice. Intact files carry a per-page list from triage with null titles. NULL means cut before 0152 or a non-PDF never opened. The 0152 column comment predates this and is corrected by the next migration. Then each trade segment knows its own legend and schedule pages. The cover-sheet prepend stays as is for the ZIP export; the plans reader builds its ledger from `page_role` instead of prepending the whole general run.

The critic flagged one risk to test on the golden set: roadway and traffic sets use `T-`, `ITS-`, `ST-`, `L-` sheet prefixes for electrical scope. The splitter may file them under `other` or `civil_drawings`. The reader therefore sends every drawing category to a sub agent (as the brief says) and the main agent decides what is electrical scope; nothing is dropped because of its category.

### 3.3 Ledger
One JSON object per run, built from the split: the sheet index transcribed from the cover sheet, the per-sheet title list from the splitter, the `legend_symbols` and `general_notes` pages (as images, at native ppi) and the spec table of contents (Division 26, 27, 28 sections with page ranges, from the spec sub agent's first pass). Every sub agent group call receives the ledger as its first content block. The ledger is capped at about 40,000 tokens; above that the legend images drop to `detail: high` and the index becomes text only.

### 3.4 Sub agents (`sub_plans_agents`) and groups (`tiered_plans_agents`)
One sub agent per output category present in the run (the 13 splitter categories plus `other`; pre-labeled uploads map to the same vocabulary). Its documents are packed into groups by token budget before anything is rendered: tokens per page come from the patch formula for drawings and 1,100 per page for PDF `input_file`. A group stays under 240,000 input tokens including the ledger and prompt (under the 272,000 2x line) and under 50 MB of `input_file` bytes. A spec book over 50 MB or over about 220 pages is cut with pypdf along section boundaries. Each group is one `llm_jobs` job (new type `plans_reader_agent`) so the queue gives parallelism, per-call retries and lease safety; a run with 30 groups is 30 jobs under one parent run row, not one 30-call job inside a 900 s lease.

Drawings are rendered page by page under the PDFium lock, straight into the request, never stored. Specs, RFP, addenda and other document files go as `input_file` PDFs. Non-PDF uploads (xlsx, docx) are converted with the existing `rfp_office_convert` path first.

Group `pages` items of kind `file` carry `ppi`, `detail` and `tiles` as null. Every group returns one JSON object (strict schema) with:
- `pages[]`: page, sheet number, title, `page_role`, `tags[]` (free metadata tags such as `panel schedule`, `site lighting`, `demo`, `one-line`, `keyed notes`), `notes[]`, `resolution_flags[]`.
- `counts[]`: description, symbol or tag, quantity, unit, sheet, location on sheet, confidence, basis (`counted_symbols | schedule_text | keyed_note | inferred`).
- `runs[]`: conduit and wire runs with size, type, from, to, length in feet, basis (scaled from drawing scale, stated, or assumed), sheet.
- `scope[]`: scope statements with sheet or spec section citations and a `relevance` of `electrical | other_trade_affecting_electrical | other`.
- `specs[]`: products, manufacturers, basis of design, approved equals, substitution rules, vendor or organization specific requirements, section and page.
- `schedules[]`: panel, fixture, equipment connection and feeder schedules transcribed as rows.
- `details[]`: detail callouts relevant to electrical with sheet and detail number.
- `questions[]`: RFIs and confirmations an estimator would raise.
Numbers carry their evidence (sheet, location, verbatim text when read from text). No pricing fields exist in any schema.

When a category has more than one group, a category roll-up call merges the group outputs (text only, cheap) into one category output and resolves duplicates across groups (a device counted on two overlapping tiles, a schedule split across two sheets).

### 3.5 Escalation
Section 2. Runs as additional `plans_reader_agent` jobs tagged `pass=2`, bounded by `PLANS_READER_MAX_ESCALATION_FRACTION` (default 0.5 of the first-pass tokens).

### 3.6 Main agent (`main_plans_agent`)
One job (type `plans_reader_main`). Input is text only: the ledger, every category output and the escalation patches, plus the project name and address. Expected 100,000 to 250,000 input tokens on a median set; a very large set goes through a two-step consolidation (per discipline, then overall) to stay under the 272,000 line. Output, as strict JSON:
- `brief`: the deep brief (scope summary, systems inventory, risks, assumptions, exclusions to carry, RFIs, missing documents, per-sheet summary).
- `boq`: the BDR BOQ template tree (DEMOLITION / NEW WORK, then WIRING with the 10% wastage note, SWITCHGEAR, OTHER ITEMS, LIGHTING FIXTURES, LIGHTING CONTROLS, LOW VOLTAGE WORK, with the standard subheads), one sheet per area when the set has areas, each row `{description, quantity, unit, sheet_refs[], material_note, caveat}` with the inline caveat vocabulary the estimators already use (`(ASSUMED)`, `(NEED TO CONFIRM SCOPE OF WORK)`, ...). Quantities are pre-wastage; the app applies the 1.1 factor on FT conductor and conduit rows and generates the fitting rows with the rules observed in the sent BOQs.
- `estimate_rows`: the same rows with `DRAWING SHEET#` per group, wastage, the nine fixed ALLOWANCES rows and `NOTE:` rows. Pricing columns stay blank.
- `markups[]`: per sheet, trench and saw-cut polylines in page-fraction coordinates with a label (`TRENCH`, `SAWCUT`, `(Need to confirm)` variants) and lengths in feet.
- `material_notes[]`: per material category, what the vendors must be told (specified manufacturers, ratings, dealer restrictions, lead-time items).

The app renders: `BOQ - COUNTS.xlsx` and `<PROJECT> - ELECTRICAL ESTIMATE.xlsx` with openpyxl from a template workbook that matches the 2026 sent files (header fills, column widths, IF/MAX serial formula, Bid Recap & Summary sheet with the WIRING and OTHER ITEMS labels the General Material extraction looks for); `Trench Markup.pdf` / `Sawcut Markup.pdf` by adding red `/Line` and `/FreeText` annotations over the source electrical sheet with pypdf; and `Deep Brief.pdf` (docx via python-docx then the existing Gotenberg conversion, or the proposal PDF path). Marked plans (count dots and legend over 11x8.5 renders with Pillow) are a later phase.

### 3.7 Storage
New tables (dev BDR database only until a release is approved): `plans_reader_runs` (status, source, render_mode, settings snapshot, token and cost totals, created_by); `plans_reader_files` (uploads and copies, label, split job link); `plans_reader_documents` (one per splitter segment or labeled file: category, source file, page range, page_meta); `plans_reader_calls` (one row per LLM call: run, agent kind, category, group index, pass, pages sent, ppi and detail per page, input / cached / output / reasoning tokens, computed cost, duration, request snapshot without images, response JSON, OpenAI response id); `plans_reader_outputs` (per category and the main output, current version); `plans_reader_deliverables` (kind, version, storage path, generated from output version, modification instruction if any); `plans_reader_chat_threads` and `plans_reader_chat_messages` (role, content, tool calls, Pro Max flag, tokens). Objects live under `plans-reader/{run_id}/{source|deliverables|renders}/` in `project-files`. `llm_call_log` gains `input_tokens`, `cached_tokens`, `output_tokens`, `reasoning_tokens` and `cost_usd` so the AI Monitor shows spend for every feature, not just this one.

### 3.8 LLM layer changes
`services/llm.py` gets an OpenAI Responses transport that accepts mixed content parts (text, `input_image` with `detail`, `input_file`), takes `reasoning={"effort": ...}`, returns the parsed JSON together with `usage` and the response id, and supports tool definitions and tool-call loops for the chat. Features `plans_sub`, `plans_main`, `plans_chat`, `plans_modify` register as `openai` with `OPENAI_PLANS_MODEL=gpt-6-astra` and `PLANS_READER_REASONING_EFFORT=medium`. The feature is OpenAI only by design: the plans features are pinned to OpenAI, so `FULL_SELF_HOSTED_LLMS_ENABLED=true` leaves them on the OpenAI key instead of sending plan sets to a 4B model (changed 2026-10-18; the first build made the feature inert in self-hosted mode, see 8.5). `_guarded` keeps logging one `llm_call_log` row per call, now with tokens.

### 3.9 Chat and Pro Max
Chat runs on the Responses API with tools the app serves: `get_brief()`, `list_documents()`, `get_output(document_or_category)`, `search_outputs(query)`, `get_deliverable(kind, version)`, `get_call_log()`, and two gated tools: `read_pages(document_id, pages, ppi)` (renders and returns the actual pages, Pro Max only, capped at `PLANS_READER_PROMAX_MAX_PAGES_PER_MESSAGE`, default 12, and the cost preview is shown before sending) and `modify_deliverable(kind, instruction)` which starts a `plans_reader_modify` job: the current deliverable JSON plus the relevant outputs go to the model with the instruction, the model returns a new JSON version, the app re-renders the file and the chat replies with a diff summary and the new version link. A normal question answers from stored outputs only. Users reference files, outputs, deliverables and the brief by name; the tools resolve the names. Chat answers are polled JSON (the frontend has no streaming path); long tool chains run as jobs with the same polling the splitter uses.

### 3.10 Roles, flags, limits
Flag `PLANS_READER_ENABLED` (default false, 404 at the router, `plans_reader` in `GET /features`, fail-closed in the frontend). Reads `require_internal` minus accountant, writes `require_writer`; the external `estimator` role never reaches it. `ai_rate_limit` on run starts and chat sends. Spend caps per run and per day (`PLANS_READER_DAILY_INPUT_TOKEN_CAP`), both visible in the AI Monitor.

### 3.11 Frontend
`/plans-reader` with: New run (drop zone or project picker, per-file label, render mode, cost preview), Run view (document tree by category with status and tokens per call, outputs viewer with page tags and notes, deliverables with versions, preview and download, the brief), a chat drawer with the Pro Max toggle and a per-message cost line, History and Calls tabs. Design system as in the rest of BDR (squared-off neumorphic navy on light, no glass, no em dashes), strings in the six locales.

## 4. Testing plan
Golden set from the production backup already on disk (`backups/BDR_Prod_2026-10-01/`, 46 submitted projects with drawings, specs, BOQ and estimate files): no production access needed. For each sampled project, run the reader on the drawing and spec files, then compare BOQ rows (description, quantity, unit) against the estimator's `BOQ - COUNTS.xlsx`, never the pricing. Report per section recall and precision, count error by device type, run-length error, and tokens and dollars per sheet and per project. First sample: Edikted @ Fashion Show (TI, 42x30), Traffic Signal Improvements (roadway, 36x24, 8 areas), YMCA Torrey Pine (site and feeders), Five Star Resolution (small TI), Hard Rock EV Parking (48x36, large), and one spec-heavy public job (Sloan Road or Cimarron-Memorial). Budget about $20 per project natively, so the first five run for about $100. The calibration artifact is updated from `plans_reader_calls` after each batch.

## 5. Decisions needed before building
1. Resolution default: adaptive native with 260 ppi escalation (recommended, about $17 per median project) or 260 ppi tiles everywhere (about $52). Or both as a run setting with adaptive as default.
2. Spend caps: per run and per day defaults, and who can override.
3. Deliverable modification: the model edits the stored JSON and the app re-renders the file (recommended, keeps the template exact) rather than the model editing xlsx or PDF bytes.
4. Marked plans: in scope for the first build or a later phase.
5. Input sources: uploads only, or also an existing project's files (read only).
6. Who can see the page: all writer roles, or executives and estimating admins only.
7. Golden-set budget: approve about $100 of OpenAI spend for the first five projects.
8. Page name and nav placement.

## 6. Build contract (2026-10-17, approved decisions)

Owner decisions: adaptive native rendering is the default with `detail: original` on every image and `reasoning.effort = medium`; each sub agent group is its own queue job under its parent run; `FULL_SELF_HOSTED_LLMS_ENABLED` excludes this feature (inert, with a note in `.env`; reversed 2026-10-18, see 8.5: the feature now keeps running on OpenAI in self-hosted mode); daily spend cap $75, IT Admins and Executives can change or reset it in Settings and both roles get an email when it is hit; modifications edit the stored JSON and the app re-renders; marked plans ship in the first build; runs start from uploads or from an existing project's files (searchable picker); every internal role can use the page (the external estimator never can); the page is named **AI Estimating**; about $100 of OpenAI spend is approved for the first golden-set run.

Module boundaries (backend, `app/`):

| Module | Owns |
|---|---|
| `supabase/migrations/0152_plans_reader.sql` | Tables, CHECKs, `llm_call_log` token columns, `bid_split_segments.page_meta`, spend settings. Applied to DEV only. |
| `services/plans_reader_schemas.py` | Every JSON contract (sub, main, deliverable specs, chat tools), vocabularies, template constants. Shared; change only with the consumers. |
| `services/llm.py` | New `complete_parts(feature, *, system, parts, schema, schema_name, reasoning_effort, tools=None, previous_response_id=None, max_tokens, timeout, settings) -> LlmResult(parsed, text, usage: LlmUsage(input, cached, output, reasoning), response_id, tool_calls)` for OpenAI Responses with mixed `input_text` / `input_image`(+detail) / `input_file` parts. `_guarded` writes tokens and `cost_usd` to `llm_call_log` through `llm_pricing.cost_for(model, usage)`. Features `plans_sub`, `plans_main`, `plans_modify`, `plans_chat` registered `openai`, `openai_only=True`: pinned to OpenAI in both modes since 2026-10-18 (8.5); `unavailable_reason` reports only the page's own `PLANS_READER_ENABLED=false`. |
| `services/llm_pricing.py` | Price table per model (gpt-6-astra 10 / 1 / 50 per 1M, 2x input and 1.5x output over 272,000 input tokens) and `cost_for`. |
| `services/plans_reader_images.py` | Patch math (`patches_for(w_px, h_px)`, `tokens_for_patches`), `native_ppi_for(sheet_w_in, sheet_h_in)` (largest ppi under 30,000 patches, capped at 260), tile planning (`plan_tiles(w_px, h_px, *, max_patches=30000, overlap=0.05)` on the already-scaled pixel size; `plans_reader_pack.plan_page` applies the ppi), `payload_bytes(parts)` against `REQUEST_MAX_BYTES`, PNG rendering of a page or region under the PDFium lock, spec PDF chunking by size and pages (`pdf_chunks`). Note: at 30,000 patches a 42x30 sheet at 260 ppi needs 4 tiles (2x2), not 6; the 3x2 grid in section 2 came from a 17,000-patch tile budget. |
| `services/plans_reader_pack.py` | Packing documents of one category into groups under `PLANS_READER_GROUP_INPUT_TOKENS` (default 240,000) and the 50 MB file cap; cost estimate for a run before it starts. |
| `services/plans_reader_agents.py` | Prompts and the per-group call for sub, roll-up, escalation and main; ledger building; writes `plans_reader_calls`, `plans_reader_groups.output`, `plans_reader_outputs`; triggers the next stage when a run's groups are all done. |
| `services/plans_reader_runs.py` | Run lifecycle: create from uploads or a project (server-side copy), label or split (splitter job with `source='plans_reader'`), documents from segments, stage transitions, cancel, totals, status payload for polling. |
| `services/plans_reader_render.py` | Files from specs: `BOQ - COUNTS.xlsx`, `<PROJECT> - ELECTRICAL ESTIMATE.xlsx` (openpyxl, template layout from the sent files, pricing columns blank, wastage 1.1 on FT conductor and conduit rows, generated accessory rows), `Trench Markup.pdf` / `Sawcut Markup.pdf` (pypdf `/Line` + `/FreeText` on the source sheet), `Marked Plans.pdf` (Pillow dots, polylines and legend over 11x8.5 renders), `Deep Brief.docx` plus PDF via the Gotenberg converter when available. |
| `services/plans_reader_chat.py` | Threads, messages, the tool loop over `CHAT_TOOLS`, Pro Max gating of `read_pages`, `modify_deliverable` as a `plans_reader_modify` job that writes a new deliverable version. |
| `services/ai_spend.py` | Daily spend since the window start (Pacific day or reset), `check_cap()` raising `SpendCapReached`, `record()` after each call, cap-hit notification to IT Admins and Executives (bell + email, once per window), settings read and update with `ai_spend_events`. |
| `services/llm_queue.py` | Job specs for `plans_reader_agent`, `plans_reader_main`, `plans_reader_modify`, `plans_reader_chat` (target = group id, run id, deliverable id, message id). |
| `services/bid_split.py` | `page_role` in `_PAGE_SCHEMA` and `_CLASSIFY_SYSTEM`, `page_meta` persisted on every segment, `source='plans_reader'` accepted with no project side effects. |
| `routers/plans_reader.py` | `/ai-estimating` routes behind `require_plans_reader` (flag) and `require_internal`; `ai_rate_limit` on run start, chat send and modify; `upload_rate_limit` on uploads. |
| `routers/ai_spend.py` | `GET /ai-spend` for any internal role (the page shows spend to everyone who can use it), `PATCH /ai-spend/cap` and `POST /ai-spend/reset` for IT Admin and Executive. `status()` carries `blocked` (true while the cap is hit or a reset is pending). |
| `core/config.py` | `PLANS_READER_ENABLED=false`, `OPENAI_PLANS_MODEL=gpt-6-astra`, `PLANS_READER_REASONING_EFFORT=medium`, `PLANS_READER_GROUP_INPUT_TOKENS=240000`, `PLANS_READER_LEDGER_MAX_TOKENS=40000`, `PLANS_READER_MAX_INPUT_TOKENS_PER_RUN=10000000`, `PLANS_READER_PROMAX_MAX_PAGES_PER_MESSAGE=12`, `PLANS_READER_ESCALATION_MAX_FRACTION=0.5`, `PLANS_READER_TILE_PPI=260`. |

Frontend: `app/(app)/ai-estimating/page.tsx` (+ `?run=` and `?tab=`), `components/aiEstimating/*`, `lib/aiEstimating.ts` types mirroring the schemas, the `ai_estimating` flag in `lib/features.ts`, nav entry in the Bidding sidebar, a Settings card "AI spend" for IT Admin and Executive, strings in the six locales.

Routes (all under `/ai-estimating`): `GET /status`; `POST /runs` {name, source, project_id?, render_mode?}; `POST /runs/{id}/files` multipart (+ `label`); `POST /runs/{id}/project-files` {project_file_ids[]}; `GET /runs/{id}/estimate` (cost preview); `POST /runs/{id}/start`; `POST /runs/{id}/cancel`; `GET /runs?status=&q=&limit=`; `GET /runs/{id}` (run, files, documents by category, groups with status and tokens, outputs summary, deliverables, totals); `GET /runs/{id}/outputs/{category|main}`; `GET /runs/{id}/documents/{doc_id}/download`; `GET /runs/{id}/deliverables/{kind}/{version}/download`; `POST /runs/{id}/deliverables/{kind}/rerender`; `GET/POST /runs/{id}/chat/threads`; `GET /runs/{id}/chat/threads/{tid}`; `POST /runs/{id}/chat/threads/{tid}/messages` {content, pro_max}; `GET /project-search?q=` (reuses the project list query); `DELETE /runs/{id}`.

## 7. Phase 2 function contracts (runs, agents, chat, routes)

These names are fixed so the three modules can be written in parallel. `sb` is the Supabase client from `get_supabase()`; every function is synchronous and safe to call from the queue worker thread.

`services/plans_reader_runs.py`
- `create_run(sb, *, name, source, project_id, render_mode, user) -> dict` inserts `plans_reader_runs` (status `created`, `reasoning_effort` and `model` from settings, `settings_snapshot` with the PLANS_READER_* values).
- `add_upload(sb, run, *, filename, content: bytes, label: str | None, user) -> dict` validates (PDF, xlsx, docx; size cap `upload_max_bytes`), converts office files to PDF through `rfp_office_convert.convert_to_pdf` when a converter is configured (else refuses with a user-safe message), uploads to `plans-reader/{run_id}/source/{uuid}-{safe}` and inserts `plans_reader_files` (status `pending`, `label`).
- `add_project_files(sb, run, *, project_file_ids: list[str], user) -> list[dict]` copies project objects server-side (`storage.copy_file`) into the run prefix, inserts files with `source_project_file_id` and a label derived from the project category (`electrical_drawing -> electrical_drawings`, `drawing -> None` (unlabeled, goes to the splitter), `specification -> specifications`, `addendum -> addenda`, `rfp -> rfp`, `marked_plans|boq|estimate|markup|quote|proposal|rfq_split|revision -> refused`, `other -> other`). Only projects the user may see (same visibility rules as GET /projects).
- `estimate(sb, run) -> dict` plans every file with `plans_reader_pack` (page sizes via `plans_reader_images.page_sizes_in`, files not yet split are estimated as drawings) and returns `{documents, groups, input_tokens, output_tokens, cost_usd, cap_remaining_usd, over_cap: bool}`; stores `estimated_input_tokens` and `estimated_cost_usd` on the run.
- `start(sb, run, *, user) -> dict` refuses unless status is `created` and at least one file exists; refuses when `ai_spend.check_cap` raises or the estimate exceeds `plans_reader_max_input_tokens_per_run`; sets `started_at`; labeled files become documents immediately; unlabeled files go to one splitter job (`bid_split_jobs` with `source='plans_reader'`, `project_id` NULL, `created_by` user; `bid_split.stage_upload` with the server-held bytes; `bid_split.dispatch(None, file_id, user_id)`), status `splitting`; with nothing to split it calls `advance`.
- `on_split_done(sb, split_job_id)` is called by `bid_split` when a `plans_reader` job finishes (hook: `bid_split._after_done`/`_after_failed` call `plans_reader_runs.on_split_file_done(file_id)` when the job source is `plans_reader`): builds documents from `bid_split_segments` (category, name, storage_path, page range, `page_meta`, sheet size from the first page), marks the run file `split`, and when every file is settled calls `advance`.
- `advance(sb, run_id)` is the state machine, idempotent and safe to call twice: `splitting -> reading` builds the ledger (`plans_reader_agents.build_ledger`), packs groups (`plans_reader_pack.pack_groups` per category, render mode from the run) into `plans_reader_groups` rows (kind `sub`, pass 1) and enqueues one `plans_reader_agent` job per group (target `group.id`, `project_id` NULL, payload `{group_id, run_id}`); while `reading`, when all pass-1 groups of a category are done and the category has more than one group, inserts one `rollup` group and enqueues it; when escalation flags exist and the escalation budget allows, inserts `escalation` groups (pass 2); when every sub, rollup and escalation group is terminal, writes `plans_reader_outputs` (scope `category`) and moves to `consolidating` with one `main` group enqueued as `plans_reader_main`; `consolidating -> rendering` when the main group is done: writes the `main` output, then renders every deliverable (`plans_reader_render`) into `plans-reader/{run_id}/deliverables/v1/...` and inserts `plans_reader_deliverables` version 1 (brief as docx plus pdf when the converter answers), then `done` (or `done_with_errors` if any group failed or any renderer raised, with the error recorded on the run). `over_cap` when `SpendCapReached` interrupts; a later `resume(sb, run)` re-enqueues the queued groups.
- `cancel(sb, run, *, user)`, `delete(sb, run, *, user)` (storage prefix sweep + row delete; refuse while splitting or reading unless canceled first), `list_runs(sb, *, status, q, limit, offset)`, `get_run_payload(sb, run_id) -> dict` with `{run, files, documents, groups (status, est and actual tokens, cost), outputs: [{scope, category, version}], deliverables, totals, queue: poll info for queued groups}`.
- `recompute_totals(sb, run_id)` sums `plans_reader_calls` into the run row.

`services/plans_reader_agents.py`
- `build_ledger(sb, run, documents) -> dict` : `{sheet_index: [{sheet_number, title, document_id, page}], legend_pages: [{document_id, page, category}], general_notes_pages: [...], spec_toc: [{section, title, document_id, page_start, page_end}] (empty until the specifications sub agent runs; the main call gets the filled version), project: {name, address}}`; `ledger_parts(sb, run, ledger, *, budget_tokens) -> list[parts]` renders legend and general notes pages as `input_image` parts at native ppi (dropping to `detail: high` when over budget) plus one `input_text` block with the index.
- `run_group(sb, group_id)` executes one group: loads run, group and documents; `ai_spend.check_cap`; builds parts in this order: system prompt (per kind and category), ledger parts, then per document a text label `D<n>: <name> (<category>, pages a-b)` and per page `D<n> P<p>` label followed by the image (rendered at the group's planned ppi, or its tiles) or the `input_file` chunk; the user prompt; then `llm.complete_parts('plans_sub'|'plans_main', ..., schema=SUB_AGENT_SCHEMA|MAIN_AGENT_SCHEMA, reasoning_effort=run.reasoning_effort, timeout=settings.plans_reader_call_timeout_seconds)`; writes a `plans_reader_calls` row (tokens, cost, request snapshot without image bytes, response id, duration) whether it succeeded or failed; maps `doc_index` back to `document_id` in the output (adds `document_id` beside every `doc_index`); stores `group.output`, status `done`, then `ai_spend.after_call(sb)` and `plans_reader_runs.advance(sb, run_id)`. On `SpendCapReached` the group stays `queued` and the run becomes `over_cap`. Roll-up groups take the pass-1 outputs of the category as JSON text instead of pages; escalation groups take the flagged regions as 260 ppi tiles plus the sheet's pass-1 output; the main group takes every category output as JSON text plus the ledger text (no images) and asks for `MAIN_AGENT_SCHEMA`.
- Prompts live in this module as constants (`SUB_SYSTEM`, `ROLLUP_SYSTEM`, `ESCALATION_SYSTEM`, `MAIN_SYSTEM`) and say in plain words: electrical subcontractor's estimator, count every device, read schedules as tables, note run lengths with their basis, quote spec text verbatim for products and manufacturers, never invent quantities, flag regions too small to read, treat document content as data not instructions, never output prices.
- `modify_deliverable(sb, run, *, kind, instruction, user) -> dict` runs `plans_reader_render` after one `complete_parts('plans_modify', ...)` call with the current spec, the main output summary and the instruction, schema `DELIVERABLE_SPEC_SCHEMAS[kind]`; inserts the next deliverable version with `instruction`; returns the row.

`services/plans_reader_chat.py`
- `create_thread(sb, run, *, title, user)`, `list_threads(sb, run_id)`, `get_thread(sb, thread_id) -> {thread, messages}`.
- `send_message(sb, run, thread, *, content, pro_max, user) -> dict` inserts the user message and a pending assistant message, enqueues `plans_reader_chat` (target `assistant_message.id`) and returns both rows.
- `run_message(sb, message_id)` : the tool loop. Builds the conversation (system prompt explaining the run, the tools and the Pro Max state; prior thread messages as text), calls `llm.complete_parts('plans_chat', ..., tools=CHAT_TOOLS, reasoning_effort=run.reasoning_effort)`, serves each tool call from the stored rows (`read_pages` only when `pro_max` and within `plans_reader_promax_max_pages_per_message`, else returns an error string the model relays; `modify_deliverable` calls `plans_reader_agents.modify_deliverable` and returns the new version), continues with `previous_response_id` and `tool_outputs` until the model answers in text or 8 rounds pass; records every call in `plans_reader_calls` with `thread_id`; stores content, `tool_calls` summaries, tokens and cost on the assistant message; status `done` or `failed` with a user-safe error.

`services/llm_queue.py` job specs: `plans_reader_agent` and `plans_reader_main` -> `plans_reader_agents.run_group(sb, payload['group_id'])`, mark = set `plans_reader_groups.status`/`error`; `plans_reader_chat` -> `plans_reader_chat.run_message(sb, payload['message_id'])`, mark on `plans_reader_chat_messages`; `plans_reader_modify` -> `plans_reader_agents.modify_deliverable` from payload `{run_id, kind, instruction, user_id}`, mark on a `plans_reader_groups` row of kind `modify`. All four join `LLM_JOB_TYPES` and `_FEATURE_BY_TYPE` (`plans_sub`, `plans_main`, `plans_chat`, `plans_modify`).

`routers/plans_reader.py` (prefix `/ai-estimating`, router dependency `require_plans_reader`, every route `require_internal`; `ai_rate_limit` on start, chat send, modify; `upload_rate_limit` on uploads). Response shapes are the dict payloads above; downloads return `{url}` from `storage.signed_url(..., download=filename)`; `GET /project-search?q=` returns `[{id, number, name, stage, files: [{id, category, filename, size_bytes}]}]` for up to 20 projects the caller may see (reuse the list-projects query path and visibility helpers).

## 8. Build log 2026-10-17

The section 6 and 7 build is in the working tree of `bdr_be` (not committed). The header status line ("Nothing is built") is superseded by this section. Migration 0152 is the only schema change; this build did not touch BDR_Prod.

Final results (integration pass, 2026-10-17):
- Full suite, `uv run pytest -q -x -p no:cacheprovider`: `7295 passed, 1 skipped, 13 warnings in 73.93s (0:01:13)`. The 13 warnings are older ones (Starlette httpx notice, HTTP_413 constant rename, short HMAC test keys).
- AI Estimating test files alone (the 12 new files below): `522 passed, 2 warnings`.
- `uv run ruff check app tests`: All checks passed.
- `uv run python -c "import app.main"`: imports clean; 21 routes mounted under `/ai-estimating` and 3 under `/ai-spend`.

### 8.1 Files added

| File | Lines |
|---|---|
| `supabase/migrations/0152_plans_reader.sql` | 344 |
| `app/services/plans_reader_schemas.py` | 523 |
| `app/services/llm_pricing.py` | 148 |
| `app/services/plans_reader_images.py` | 525 |
| `app/services/plans_reader_pack.py` | 583 |
| `app/services/plans_reader_render.py` | 2290 |
| `app/services/plans_reader_agents.py` | 1959 |
| `app/services/plans_reader_runs.py` | 2602 |
| `app/services/plans_reader_chat.py` | 1798 |
| `app/services/ai_spend.py` | 555 |
| `app/routers/plans_reader.py` | 923 |
| `app/routers/ai_spend.py` | 85 |
| `scripts/plans_reader_golden.py` | 827 |
| `docs/PLANS_READER.md` | this doc |
| `tests/test_plans_reader_runs.py` | 1575 |
| `tests/test_plans_reader_agents.py` | 648 |
| `tests/test_plans_reader_chat.py` | 1159 |
| `tests/test_plans_reader_routes.py` | 1462 |
| `tests/test_plans_reader_render.py` | 1352 |
| `tests/test_plans_reader_images.py` | 415 |
| `tests/test_plans_reader_pack.py` | 299 |
| `tests/test_plans_reader_golden.py` | 426 |
| `tests/test_ai_spend.py` | 944 |
| `tests/test_llm_parts.py` | 741 |
| `tests/test_llm_pricing.py` | 128 |
| `tests/test_bid_split_page_roles.py` | 764 |

### 8.2 Files changed

| File | Change |
|---|---|
| `app/services/llm.py` (+581/-) | `complete_parts`, `LlmUsage` (with `input`/`cached`/`output`/`reasoning` aliases), `LlmResult` (+`model`, `cost_usd`), `last_usage`/`last_cost_usd`, `LlmBadOutputPermanent`, `LlmLeaseLost`, four `openai_only` plans features, `unavailable_reason`, usage and cost logging |
| `app/services/llm_gate.py` (+71) | `log_call` token and `cost_usd` columns, one retry without them on a pre-0152 database |
| `app/services/llm_health.py` (+13) | labels; `check()` skips features that are off by design |
| `app/services/llm_queue.py` (+407) | four plans job types, specs and fenced marks, separate plans claim pass (`_claim_plans`), modify runner with `base_version` and paid-call guard |
| `app/services/bid_split.py` (+413) | `page_role`, `page_meta` on every segment, `source='plans_reader'` with no project effects, `on_split_file_done` hook, `sweep_stale_files`, `dispatch(None, ...)` thread fallback |
| `app/routers/bid_splitter.py` (+108) | plans_reader jobs hidden from the default list and refused (409) on the six edit routes |
| `app/services/storage.py` (+72) | `build_plans_reader_path`, `delete_plans_reader_prefix` |
| `app/services/notification_email.py` (+6) | `ai_spend.cap_hit` email type and Settings deep link |
| `app/core/config.py` (+116) | PLANS_READER_* settings, `PLANS_READER_LEASE_HEADROOM_SECONDS`, `_validate_plans_reader` |
| `app/core/features.py` (+17) | `ai_estimating` flag, `require_plans_reader` |
| `app/core/error_codes.py` (+7), `docs/ERROR_CODES.md` (+12) | `ai_estimating_not_actionable` (409), `ai_estimating_spend_cap` (409), `ai_estimating_unavailable` (503) |
| `app/models/schemas.py` (+100) | `AiEstimating*` request and status models |
| `app/main.py` (+8) | mounts `ai_spend.router` and `plans_reader.router` (own flag, not BIDDING) |
| `.env.example` (+36), `.env` (local, gitignored) | AI Estimating block; note that the self-hosted switch leaves the feature on OpenAI |

Older tests changed (each pinned behaviour the contract intentionally changed):
- `tests/conftest.py`: sets `PLANS_READER_ENABLED=true` so the AI Estimating routes are reachable in the suite.
- `tests/test_feature_flags.py`: `test_all_sub_apps_enabled_by_default` expects `ai_estimating` and asserts the setting defaults to false; `test_rfp_ingest_settings_guard_refuses_each_bad_combination` turns plans_reader off so its own lease validator does not fire first.
- `tests/test_llm_health.py::test_server_up_and_serving_the_configured_model_is_green`: the self-hosted fixture turns `plans_reader_enabled` off, so only features that are not `openai_only` are graded.
- `tests/test_llm_target.py::test_override_box_ignores_pins_but_keeps_off`: skips `openai_only` features (the box switch never touches them).
- `tests/test_llm_queue.py`: `_settings` defaults `plans_reader_enabled=False`; the job-type test asserts the plans types are not in `LLM_JOB_TYPES`.
- `tests/test_llm_routing.py`: self-hosted test fixed for the new features plus new openai_only tests.
- `tests/test_bid_splitter.py`, `tests/test_bid_split_corrections.py`: a reply without `page_role` reads as `other`; null `page_meta` where the stored model pages do not cover the range.
- The integration pass itself changed no test and no code: ruff, the full suite and the import check were already green.

### 8.3 Where the code differs from sections 3, 6 and 7 (the code wins)

1. Images: `patches_for(w_px, h_px)` (alias `patches`) and `plan_tiles(w_px, h_px, *, max_patches=30000, overlap=0.05)`; the ppi is applied in `plans_reader_pack.plan_page`. Under the fewest-tiles rule a 42x30 sheet at 260 ppi is 3 full-height strips, 36x24 is 2 and 48x36 is 4 (not the section 2 grids or the "4 tiles" note in section 6). A lower `max_patches` (17,000) gives 3x2.
2. Group items: `kind: file` items carry `ppi`, `detail` and `tiles` as null; image items add `w_in` and `h_in`; escalation items add `regions`, `reasons` and `source_group_id`. The column comments were corrected in 0153.
3. `page_meta` entries are `{page, pdf_page, prepended, title, page_role, confidence}` in segment-PDF order; intact files get a per-page list from triage with null titles (`spec_text` for specifications, rfp and addenda). Output `page` is the page of the document's stored PDF, cover pages included; trade documents skip prepended pages and the General document reads them. Chat `read_pages` uses `page - 1`.
4. LLM: `LlmUsage(input_tokens, cached_tokens, output_tokens, reasoning_tokens)`; `active_model()` returns the OpenAI name for plans features whose flag is off instead of raising; `complete_parts` raises `LlmBadOutputPermanent` on an output-limit cut-off, refusal or content filter.
5. Queue: the plans types are not in `LLM_JOB_TYPES`. `PLANS_JOB_TYPES` have their own claim pass and capacity (`plans_reader_concurrency`, default 2), chat and modify are claimed first, agent and main jobs never take the last slot, and the pass runs only while `PLANS_READER_ENABLED` is on. `ALL_JOB_TYPES` matches the 0152 CHECK. Failed or canceled groups and failed answers report `done`, so the AI Monitor's Retry refuses them.
6. Spend: `record` is an alias of `after_call`; `status()` adds `blocked` (cap reached, or a step was refused this window; it drives the Settings banner only, since `check_cap` still accepts a smaller step that fits); the run estimate's `over_cap` means this run cannot start (`cap_hit` or `cost_usd_max` above what is left) and the frontend disables Start and modify only on `cap_hit`; `GET /ai-spend` is any internal role, PATCH and POST are IT Admin and Executive, and all three 404 while `PLANS_READER_ENABLED` is off.
7. Runs: `RunRefused(ValueError)` carries `status_code`. `start` with nothing to split kicks `advance` on a daemon thread. The brief PDF sits next to the docx under the same key with `.pdf` (`brief_pdf_path`), with no deliverable row of its own; the payload adds `pdf_available` and `pdf_filename`. A failed main group ends the run `failed`. `rerender_deliverable(sb, run, kind, *, user=None)` queues a `plans_reader_modify` job (`instruction` null, `rerender` true) on a `modify` group and returns `{group, queue, kind, version}`; the route answers 202 under `ai_rate_limit`. One queued or running modify group per run (0153 partial unique index; `claim_change_slot` turns 23505 into 409). `delete` first moves the run to `canceled` with a compare-and-set, re-checks for running groups and pending answers (409 `MSG_DELETE_BUSY`), and restores the old status if the storage sweep fails (503). `start()` undoes a crash part way (queued splitter jobs canceled, documents removed, files back to pending, run back to `created`, 503 `MSG_START_FAILED`). A main group that fails for good gets one retry at the next lower reasoning effort (pass 2); only the attempt whose answer was used counts in `groups_total`. Labeled drawing sets get a derived `page_meta` (sizes, role `other`, `derived: true`) so their sheets reach the ledger. Group start times are paced over 60 s windows to `plans_reader_tokens_per_minute` (default 800,000; 0 off) and each job payload carries `est_input_tokens`. `resume` takes `over_cap` and stalled `reading`, `consolidating` or `rendering` runs. While first-pass groups are planned, `ledger` briefly holds `{"planning_claim": {"at": ...}}`. Split files pass through `settling` (0153) while their documents are written; a database without 0153 falls back to `converted`, and both read as settling. `resume` on a rendering run needs 600 s without change and skips the cap check. Polls re-enqueue orphan groups and repair stuck rendering runs. The pre-split cost preview assumes the worst-case ledger in every group. Escalation is one group per flagged sheet, tiles plus ledger text, and is skipped when the sheet was already read at 260 ppi or more.
8. Agents: category outputs add `escalation_patches`, `unresolved_flags` and `source`; roll-up and main calls carry the ledger as text only. Schemas have no position fields, so counts put `at (x,y)` in `location` and runs put `route: (x,y)` in `notes`. Every call sends `max_tokens` = 2x the planned output plus 32k reasoning headroom, capped at 128k. `modify_deliverable(sb, run, *, kind, instruction, user, group_id=None, thread_id=None)`.
9. Chat: a change made from chat calls `modify_deliverable` inline (not a `plans_reader_modify` job) but takes the run's change slot through `claim_change_slot` as a `running` modify group that records the chat message id; `release_orphan_changes` fails such a group once its answer is no longer pending or running. Only for writer roles, only when the user's message asks for or confirms a change, and only on a `done` or `done_with_errors` run. One pending or running assistant message per thread (0153 partial unique index; the 23505 becomes the busy refusal). A pending answer older than 15 minutes with no live job is released by the service (`_live_answers` / `_is_stale`), never by the router. There is no `get_call_log` tool. Message rows have no cached or reasoning token columns. Run, document and deliverable names go in a user-role data part, not the system prompt.
10. Routes: reads use `require_internal` (the read-only Accountant can view runs and spend) and every write (create, upload, project files, start, cancel, resume, delete, rerender, modify, chat thread create and send) uses `require_writer`. Added `POST /runs/{id}/resume` and `POST /runs/{id}/deliverables/{kind}/modify` (payload carries `base_version`, cap checked first, `claim_change_slot`). `/status` adds `unavailable_reason`, `promax_max_pages_per_message`, `upload_max_mb`, `render_modes` and `spend` (with `blocked`). `GET /runs/{id}` adds `calls` (newest 2,000, newest first, no request snapshot) and per-group `document_pages`; modify groups carry `change` (rerender, chat or modify), `deliverable_kind` and `deliverable_version`. The brief PDF downloads through `.../deliverables/brief/{v}/download?format=pdf` (404 when it was not produced). The estimate route works after start and returns `cap_remaining_usd` and `over_cap`. Rerender works with no existing deliverable and writes an `ai_estimating.deliverable_rerendered` audit row with `group_id`. Output reads go straight to `plans_reader_outputs`.
11. Render: `render_markup_pdf(sources, sheets, *, kind, line_width=7.0, font_size=36.0)` with `markup_documents(sheets, *, kind)`; 7 pt lines and 36 pt labels as in the sent markups; `deliverable_filename` also takes `brief_pdf`.

### 8.4 Open items (not built)

Closed by 0153 and the 2026-10-17 follow-up pass: `plans_reader_concurrency` and `plans_reader_tokens_per_minute` settings, modify groups polled as `plans_reader_modify`, delete compare-and-set with busy re-check and undo, the router's own chat pending check (removed), the 0153 indexes and `settling` status, the modify spec saved before rendering, derived ledger entries for labeled drawing sets, `pypdf>=5.0,<7`, rerender as a queued job, pacing, the main-group retry, start crash recovery, `calls` and `document_pages` built by the service, `SpendStatus.blocked` on the frontend, and the pypdfium page-close fix in `page_sizes_pt`, `render_page_png` and `pdf_split.render_pages_jpeg`.

Live golden runs 2026-10-18 (dev, port 5061 test backend, prod-backup files): Five Star Resolution 26.8.7172 (25 sheets, no specs) ran end to end: 15 calls, 1,462,661 input / 173,812 output tokens, $25.99, BOQ 18 of 56 estimator lines matched (15 exact, 5.0 percent mean error, lighting 13 of 13); 5-sheet groups landed within 0.5 percent of their planned tokens. YMCA Torrey Pines 26.8.7177 (7 sheets, a scanned 5-page addendum and a 38-page exhibit PDF as input_file) ran in 14.7 minutes: 5 calls, 415,486 in / 49,925 out, $6.65, BOQ 9 of 30 lines matched (3.6 percent mean error) and a trench markup on the site plan; scanned PDF pages billed about 8,400 tokens each against the 1,036 text-page calibration, so the file estimate should scale with page image density. Edikted 26.8.7170 was refused at start by the daily cap (worst case $70.38 against $52.66 left), as designed. The main prompt gained estimator-style row rules (short item names, breakers as separate rows, receptacles split by type, branch wire and conduit by size) from the Five Star misses. Harness: `scripts/plans_reader_live_run.py` (token refresh, `--run-id` attach, scores the rendered workbook).

Still open:
- Markups: the main agent produced no trench or saw cut markup sheet for Five Star although the estimator drew a 20 FT saw cut; the prompt could ask for a markup whenever the BOQ carries saw cut or trench rows.
- Marked plans place one marker per legend row (an area subtotal), not one dot per counted device, because the sub agents report positions only for some counts.
- `llm_pricing` has no Anthropic prices, so splitter calls log `cost_usd` null.

- `plans_reader_images`: a sizes-only chunk planner, so planning a large spec book does not hold every chunk's bytes.
- `plans_reader_schemas.CHAT_TOOLS`: the `read_pages` description should say pages are 1-based within the document, cover pages included.
- No two-step main consolidation over 272k input tokens (3.6) and no size guard on very large roll-up inputs; both need a design decision.
- Labeled drawing sets still get no legend pages in the ledger (no page classification runs on a file the user labeled); a classify-only `bid_split.page_roles(pdf_bytes, category)` hook would close this.
- Accountant role: reads allowed, writes refused (`require_writer`); owner decision on whether the Accountant should see the page at all.
- Migrations 0152 and 0153 were applied to DEV through `execute_sql`, so the dev `supabase_migrations` ledger has no rows for them; the release-time `apply_migration` to staging and prod records them.

### 8.5 Change 2026-10-18: AI Estimating runs in self-hosted mode too

Owner decision: "AI estimating should still run when FULL_SELF_HOSTED_LLMS_ENABLED is true." The first build (section 6) made the feature inert under that flag. Now `openai_only` in `services/llm.py` means pinned to OpenAI: `resolve` returns the OpenAI route for `plans_sub`, `plans_main`, `plans_modify` and `plans_chat` in both modes, every other feature still moves to the box, and a bid set is still never sent to a self-hosted model. `unavailable_reason` no longer mentions the switch; it reports only the feature's own `PLANS_READER_ENABLED=false` (`PLANS_READER_OFF_MESSAGE`, through the new `_Feature.switch` field), and the health snapshot leaves the plans features out while that flag is off so a box-only deployment never shows amber for them. With the flag on, the snapshot probes OpenAI alongside the box in self-hosted mode. A missing `OPENAI_API_KEY` is still "not configured" (503 on start, modify, chat and resume; status `enabled: false`). Dev `.env` keeps `FULL_SELF_HOSTED_LLMS_ENABLED=true` and the page now runs against OpenAI there without a second backend.

### 8.6 Change 2026-10-18: every deliverable previews in the app

Owner request: "users should be able to preview the deliverables." Before this the Deliverables tab previewed only the PDF kinds (markups, marked plans) and the brief's PDF copy; the BOQ and estimate workbooks downloaded, because AI Estimating had no office-derivative route (the frontend's `AI_ESTIMATING_FILES` strategy had no `fetchPreviewUrl`).

- `plans_reader_render.office_pdf_bytes(content, filename)` generalizes `brief_pdf_bytes` (now a one-line wrapper): any office deliverable through the configured converter (Gotenberg or Graph), None on any failure. The file name matters: Gotenberg renders an `.xlsx` as one PDF page per sheet. Both workbooks keep a blank first `Sheet1` to match the estimator's sent files; LibreOffice prints it as a 48 x 0 pt page, so `_drop_blank_pages` removes any converted page under 36 pt in either dimension (live check against the local Gotenberg: the BOQ copy is one 505 x 873 pt page, the estimate copy two pages, 1276 x 975 and the recap 861 x 787).
- `plans_reader_runs`: `PDF_COPY_KINDS = ("boq", "estimate", "brief")`; `pdf_copy_path` (was `brief_pdf_path`, kept as an alias) strips `.docx` or `.xlsx` and adds `.pdf`; `pdf_copy_filename` names the copy after the sent file ("BOQ - COUNTS.pdf", "Rev 2 - <NAME> - ELECTRICAL ESTIMATE.pdf", "Deep Brief.pdf"). `_render_bytes` now produces the copy for the two workbooks as well, stored beside the file at render time; the run payload carries `pdf_filename` and `pdf_available` (checked for the latest version of each office kind).
- `pdf_copy_status(run, row)` answers the viewer's states: a PDF kind is `ready` with its own link; an office kind is `ready` with the copy's link when it exists, else (converter configured) the copy is built on a daemon thread, one per object and process (`_PDF_COPY_BUILDING`), and the answer is `pending`; a build without a PDF is remembered in `_PDF_COPY_FAILED` and reads `failed` for `PDF_COPY_RETRY_SECONDS` (300); no converter reads `unavailable`. This covers versions rendered while the converter was down and versions rendered before this change.
- Route `GET /runs/{id}/deliverables/{kind}/{version}/preview-url` (`require_internal`) returns that dict. `?format=pdf` on the download route now works for every office kind (404 `MSG_NO_PDF_KIND` on a PDF kind, `MSG_NO_BRIEF_PDF` while no copy exists).
- Frontend: `AI_ESTIMATING_FILES.fetchPreviewUrl` calls the route, so `canPreviewInline` is true for xlsx and docx and the Preview button appears on every version; `useFilePreview` polls while `pending` (2.5 s, up to 12 polls) and falls back to the download on `failed` or `unavailable`. The separate "PDF" button on the brief is gone (Preview opens the same copy; the viewer offers Download original); the `aiEstimating.deliverables.pdf` and `.pdfHint` strings were removed from the six locales.
- Tests: `test_plans_reader_routes` (PDF download for office kinds, preview-url states), `test_plans_reader_runs` (copies for the workbooks, payload fields, path and name helpers), `test_plans_reader_render` (`office_pdf_bytes`).
