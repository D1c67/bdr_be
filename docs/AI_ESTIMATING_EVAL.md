# AI Estimating eval harness (WS0)

How the measuring stick of the autonomous AI Estimating build works: bundles, replay, the acceptance suite, the baseline report and the live runner. The build spec (`docs/AI_ESTIMATING_BUILD_SPEC.md`) owns every threshold (section 3.5), every metric (3.6) and the live-spend rules (0.4); this page says how to run the pieces. No dollar figures from real projects appear here; everything with dollars lives under `EVAL_ROOT` (default `/Users/thomasmoore/Desktop/BDR/backups/eval`, outside both git repos).

## Pieces

| Piece | Module | Script | What it does |
|---|---|---|---|
| Manifest | `app/services/eval_manifest.py` | `scripts/eval_manifest_build.py` | tiers (smoke, core, holdout, pool), the 66 golden ids, the blind guard, `excluded_projects` |
| References | `app/services/eval_reference.py` | `scripts/eval_reference_build.py` | `reference.json` per case from the production backup (estimator BOQ and estimate, G3 sent, Verify, labor notes, quotes, adjudications) |
| Scorer v2 | `app/services/plans_reader_score.py` | `scripts/eval_score.py` | `classify_line`, many-to-one `assign`, count lines exact, length bands, dollar coverage and weighted error, bid totals |
| Bundles | `app/services/eval_bundle.py` | `scripts/eval_bundle.py` | one stored run on disk (weekend layout); load, import the weekend export, export a dev run |
| Replay | `app/services/plans_reader_replay.py` | `scripts/eval_replay.py` | the stage spine of spec 4.1 over a bundle, at $0 |
| Acceptance | `app/services/plans_reader_acceptance.py` (+ `eval_text_anchors.py`) | `scripts/eval_acceptance.py` | E1 to E25 and GC1 to GC4 as detectors over replay artifacts; the self-test against the 3.5 Baseline column |
| Reports | | `scripts/eval_report.py` | the $0 baseline table (step 14), sweep reports, re-scoring after a matcher change |
| Ledger | `app/services/eval_ledger.py` | `scripts/eval_ledger.py` | the $400 live-spend ledger: sweeps, allocations, the guard in `llm._guarded` |
| Cassette | `app/services/llm_cassette.py` | | record and replay of provider calls at the transport boundary |
| Runner | | `scripts/eval_run.py` | golden cases against the dev sweep backend (extends `scripts/plans_reader_live_run.py`) |

## Bundles

A bundle is a directory in the layout of the 7154 weekend export (`EVAL_ROOT/bundles/7154/ff8fad25`, see its `INDEX.md`): `run.json`, `files.json`, `documents.json`, `groups.json` (rows without output), `groups/<kind>_<category or none>_<index>_p<pass>.json` (one group output each), `outputs/category_<category>_v<n>.json` and `outputs/main_none_v<n>.json`, `deliverables.json` (rows without spec_json) with `deliverables/<kind>_v<n>.json`, `calls.json` (request snapshots carry hashes, never image bytes), `chat.json`, an optional `artifacts/` directory of stage artifacts a later stage wrote (`priced_estimate.json`, `gate_report.json`, ...), an optional `inputs/` directory of source PDFs, and `bundle.json` (case, source, run id, export time in Pacific, sha256 of every member).

Source PDFs are not copied into a bundle: `RunBundle.doc_bytes(document_id)` maps the run file's `source_project_file_id` through the backup's `data/project_files.json` (category and storage path from the row, never from the folder), cuts a splitter segment to its pages, and only then looks in `inputs/`.

```
uv run python scripts/eval_bundle.py import-weekend --src $EVAL_ROOT/bundles/7154/ff8fad25     # adds bundle.json in place
uv run python scripts/eval_bundle.py export --run-id <dev run uuid> --case 7172               # dev database only
uv run python scripts/eval_bundle.py show --bundle $EVAL_ROOT/bundles/7154/ff8fad25
uv run python scripts/eval_bundle.py verify --bundle $EVAL_ROOT/bundles/7154/ff8fad25
```

`export` refuses BDR_Prod and BDR_Staging. The Five Star (7172) and YMCA (7177) dev-run exports are wave 2 work: find the latest done run of each on dev (spec WS0 step 7 gives the query), record the ids in the build log, then run `export`.

## Replay

`plans_reader_replay.STAGE_ORDER` mirrors spec 4.1: `plan, merge, recount, derive, consolidate, enforce, gate, evidence, render, price, review, score`. Each stage is a registry entry (`register_stage(name, fn, version=..., reads=..., writes=..., llm=False, owner=...)`) that its owning workstream fills in: plan WS1; merge, consolidate and gate WS4; recount and review WS7 (stored outputs or a cassette, never live); derive WS3 and WS5; enforce WS5; evidence WS8; price WS6; render and score WS0. Until a real entry lands, a legacy entry carries the stored outputs forward:

- `plan`: the stored page_meta and ledger;
- `merge`: rebuilds each category output from the stored group outputs with today's `plans_reader_runs._category_output` and notes any difference from the stored output;
- `consolidate`: the stored main output cut into the deliverable specs (`deliverable_spec`);
- `render`: `render_boq_xlsx`, `render_estimate_xlsx` and the brief docx with `office_pdf_bytes` patched out; files only under the replay's out_dir; the rendered BOQ rows are an artifact (`rendered.boq_rows`); marked plans only with `--render-marked`;
- `score`: scorer v2 against the case reference (adjudicated and raw);
- every other stage: a pass-through that records a note.

A stage function takes `(ctx, artifacts)` and returns the artifacts it writes; `ctx.sb` is always None and documents come through `ctx.docs` (`LocalDocBytes`, the `_DocBytes` interface over the bundle). `replay()` refuses every provider call while it runs, so a $0 replay can never spend; a stage registered with `llm=True` may only answer from a cassette. `replay.json` records the stage table (mode, version, owner, input and output sha256, seconds), the code version and `MATCHER_VERSION`.

```
uv run python scripts/eval_replay.py --bundle $EVAL_ROOT/bundles/7154/ff8fad25 [--from-stage merge] [--stage consolidate=stored]
```

Modes per stage: `rerun` (the registry entry), `stored` (carry the bundle's artifacts), `skip`. There is no hybrid live mode: a live probe is a real dev run on a page-subset PDF whose bundle is exported and replayed.

## Acceptance suite

`plans_reader_acceptance.CHECKS` (the registry of `estimating_contracts`) holds one detector per row of spec 3.5. A detector reads the artifacts the row's Reads column names, the case reference, the scorer result and, for E9 and E13, the thresholds resolved from `EVAL_ROOT/references/7154/expected.json`. It returns `pass | fail | skip | safe_refusal | error` with measured values per part:

- `fail` when any measurable part misses its threshold;
- `skip` only when nothing could be measured because an artifact is absent, with the reason; rows where the absent artifact is itself the failure (no priced estimate on E1, E4, E9, E13, GC1, GC4) fail;
- `safe_refusal` for a refusal that maps to a real defect (E15 on cases other than 7154, GC3);
- `error` when the detector crashed (`Check.run` catches it; never a pass).

Checks that need a text layer (E17, the E24 fallback) read the source PDFs through WS1's `pagefacts` package via `eval_text_anchors`; a page without a usable text layer or without a scale is skipped with a note. E17 records two distances per mark: to the nearest anchor of the mark's tag (the 3.5 criterion) and to the nearest device anchor of any family (the method whose numbers the Baseline column quotes).

```
uv run python scripts/eval_acceptance.py --bundle <bundle> [--only E2,E12] [--json out.json]
uv run python scripts/eval_acceptance.py --self-test          # every row vs the 3.5 Baseline column on the weekend bundle
```

The self-test (`plans_reader_acceptance.self_test`, golden test `tests/test_plans_reader_acceptance.py`) replays the weekend bundle and asserts each row's status and baseline value (`BASELINE_7154`). E25 is the one row whose baseline FAIL is under the v1 scorer: the v2 detector passes on the same bundle, and `tests/test_eval_golden_7154.py` pins the v1 failure. GC2 and GC3 skip on the weekend bundle (no scope report; nothing refused).

`smoke_gate(results)` gives the 7154 smoke verdict of spec 1.3 item 1 over the rows marked "gates" (a safe_refusal counts as a fail).

## The $0 baseline (step 14)

```
uv run python scripts/eval_report.py baseline        # writes EVAL_ROOT/reports/baseline_7154.md
```

The table lists every 3.5 row with its Baseline column text, the detector's status on the weekend bundle, whether the measured values match the baseline, the smoke and replay flags, the measured summary and the evidence, plus the replay stage table and the rows that skipped. It is the "before" table every workstream reports against; re-run it after a scorer or detector change.

## Live runs (dev only; spec 0.4 and 8.5)

`scripts/eval_run.py` extends `scripts/plans_reader_live_run.py` (its `_Auth`, `_project`, label map, upload and poll code) with the eval flow: blind guard over the uploads, a run named `EVAL <case>` with `eval_case_id` and `eval_sweep_id`, the case's quantity inputs from the backup with their labels, the cost estimate (a case whose maximum exceeds the sweep's remaining budget minus the reserve is `skipped_budget`), start, poll, export of the bundle, the score stage and the acceptance suite, `results.json` under the sweep directory, and the sweep closed at the end.

```
uv run python scripts/eval_run.py --case 7154 --dry-run --base-url http://127.0.0.1:5061   # create, upload, estimate: $0
uv run python scripts/eval_run.py --tier smoke --workstream WS0 --purpose smoke --budget 45
uv run python scripts/eval_run.py --cases 7154 --sweep <open sweep id> --repeat 2            # the noise repeat
uv run python scripts/eval_run.py --tier holdout --holdout-final --workstream WS0 --purpose holdout --budget 73
```

Rules the script enforces: `SUPABASE_URL` must point at the dev project; a live run needs an open sweep (`--sweep`) or `--workstream --purpose --budget` to open one; the backend must report the ledger enforced; the holdout tier needs `--holdout-final` and a clean committed tree. `--no-start` creates and uploads only (S2 seeding); `--no-cassette` records in the sweep config that no cassette is used. Start the sweep backend exactly as spec 8.5 says, under `caffeinate`, with every poller off and `LLM_QUEUE_CLAIM_TYPES` restricted.

## Tests

- `tests/test_eval_bundle.py`, `tests/test_plans_reader_replay.py`, `tests/test_plans_reader_acceptance.py`, `tests/test_eval_run_cli.py` run on synthetic fixtures (`tests/eval_fixtures.py`: made-up ids, blank PDFs, unit costs of 1.0).
- Golden tests (`pytest.mark.golden`, skipped when `EVAL_ROOT` or the backup is missing) replay the weekend bundle, reproduce its stored outputs and assert the 3.5 Baseline column.
- A replay test patches every `llm` entry point to raise and still replays end to end: the harness never calls a provider.
