-- 0101 — Bidding restructure: vendor selection becomes its own step, and Markup
-- becomes its own lane.
--
-- Picking the winning vendor moves OFF Receive Quotes onto a new `select_vendors`
-- task. Receive Quotes keeps only what it is actually for: taking quotes in
-- (ingested or hand-entered), verifying each figure, and answering the sales-tax
-- question. Because a winner can be picked from the quotes that HAVE arrived,
-- Receive Quotes no longer has to be finished at all — Select Vendors is what
-- gates Send Out.
--
-- Markup leaves the labor lane so it can be worked while labor numbers are still
-- outstanding (its per-section boxes lock individually instead). The board grows
-- from four lanes to six, matching the flow:
--
--   intake           [intake, go_no_go, to_estimator]
--   material_numbers [estimate_received, rfqs, receive_quotes]
--   labor_numbers    [labor_numbers]
--   select_vendors   [select_vendors]
--   markup           [markup]
--   send_out         [gc_pricing, verify, send_out, submitted, bid_outcome]
--
-- ENUM LABELS ONLY. Postgres allows ADD VALUE inside a transaction only as long as
-- the new label is not USED later in that same transaction, so every statement that
-- writes one of these labels lives in 0102 (the same split 0042_gc_pricing_stage.sql
-- used when `gc_pricing` was added).

-- The new pipeline task, ordered between Receive Quotes and Labor Numbers to match
-- the flow. Physical enum order is cosmetic — the app sequences on the `order` ints
-- in services/workflow.STAGES — but it is kept in step for anything that ORDER BYs
-- the enum directly.
alter type project_stage add value if not exists 'select_vendors' before 'labor_numbers';

-- Two new LANE labels. `select_vendors` and `markup` now each name both a task and
-- the single-task lane holding it — exactly as `labor_numbers` and `send_out`
-- already name both a task and a lane.
alter type project_category add value if not exists 'select_vendors' after 'material_numbers';
alter type project_category add value if not exists 'markup' after 'labor_numbers';

notify pgrst, 'reload schema';
